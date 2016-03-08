from __future__ import division

import platform
import pytest
import time
import unittest2
from uuid import uuid4
import sys

from pykafka import KafkaClient
from pykafka.exceptions import MessageSizeTooLarge, ProducerQueueFullError
from pykafka.partitioners import hashing_partitioner
from pykafka.protocol import Message
from pykafka.test.utils import get_cluster, stop_cluster, retry
from pykafka.common import CompressionType
from pykafka.producer import OwnedBroker


class ProducerIntegrationTests(unittest2.TestCase):
    maxDiff = None
    USE_RDKAFKA = False
    USE_GEVENT = False

    @classmethod
    def setUpClass(cls):
        cls.kafka = get_cluster()
        cls.topic_name = b'test-data'
        cls.kafka.create_topic(cls.topic_name, 3, 2)
        cls.client = KafkaClient(cls.kafka.brokers, use_greenlets=cls.USE_GEVENT)
        cls.consumer = cls.client.topics[cls.topic_name].get_simple_consumer(
            consumer_timeout_ms=1000)

    @classmethod
    def tearDownClass(cls):
        cls.consumer.stop()
        stop_cluster(cls.kafka)

    def _get_producer(self, **kwargs):
        topic = self.client.topics[self.topic_name]
        return topic.get_producer(use_rdkafka=self.USE_RDKAFKA, **kwargs)

    def test_produce(self):
        # unique bytes, just to be absolutely sure we're not fetching data
        # produced in a previous test
        payload = uuid4().bytes

        prod = self._get_producer(sync=True, min_queued_messages=1)
        prod.produce(payload)

        message = self.consumer.consume()
        assert message.value == payload

    def test_sync_produce_raises(self):
        """Ensure response errors are raised in produce() if sync=True"""
        with self._get_producer(sync=True, min_queued_messages=1) as prod:
            with self.assertRaises(MessageSizeTooLarge):
                prod.produce(10 ** 7 * b" ")

    def test_produce_hashing_partitioner(self):
        # unique bytes, just to be absolutely sure we're not fetching data
        # produced in a previous test
        payload = uuid4().bytes

        prod = self._get_producer(
            sync=True,
            min_queued_messages=1,
            partitioner=hashing_partitioner)
        prod.produce(payload, partition_key=b"dummy")

        # set a timeout so we don't wait forever if we break producer code
        message = self.consumer.consume()
        assert message.value == payload

    def test_async_produce(self):
        payload = uuid4().bytes

        prod = self._get_producer(min_queued_messages=1, delivery_reports=True)
        prod.produce(payload)

        report = prod.get_delivery_report()
        self.assertEqual(report[0].value, payload)
        self.assertIsNone(report[1])

        message = self.consumer.consume()
        assert message.value == payload

    def test_recover_disconnected(self):
        """Test our retry-loop with a recoverable error"""
        payload = uuid4().bytes
        prod = self._get_producer(min_queued_messages=1, delivery_reports=True)

        # We must stop the consumer for this test, to ensure that it is the
        # producer that will encounter the disconnected brokers and initiate
        # a cluster update
        self.consumer.stop()
        for t in self.consumer._fetch_workers:
            t.join()
        part_offsets = self.consumer.held_offsets

        for broker in self.client.brokers.values():
            broker._connection.disconnect()

        prod.produce(payload)
        report = prod.get_delivery_report()
        self.assertIsNone(report[1])

        self.consumer.start()
        self.consumer.reset_offsets(
            # This is just a reset_offsets, but works around issue #216:
            [(self.consumer.partitions[pid], offset if offset != -1 else -2)
             for pid, offset in part_offsets.items()])
        message = self.consumer.consume()
        self.assertEqual(message.value, payload)

    def test_async_produce_context(self):
        """Ensure that the producer works as a context manager"""
        payload = uuid4().bytes

        with self._get_producer(min_queued_messages=1) as producer:
            producer.produce(payload)

        message = self.consumer.consume()
        assert message.value == payload

    def test_async_produce_queue_full(self):
        """Ensure that the producer raises an error when its queue is full"""
        with self._get_producer(block_on_queue_full=False,
                                max_queued_messages=1,
                                linger_ms=1000) as producer:
            with self.assertRaises(ProducerQueueFullError):
                while True:
                    producer.produce(uuid4().bytes)
        while self.consumer.consume() is not None:
            time.sleep(.05)

    def test_async_produce_lingers(self):
        """Ensure that the context manager waits for linger_ms milliseconds"""
        linger = 3
        with self._get_producer(linger_ms=linger * 1000) as producer:
            start = time.time()
            producer.produce(uuid4().bytes)
            producer.produce(uuid4().bytes)
        self.assertTrue(int(time.time() - start) >= int(linger))
        self.consumer.consume()
        self.consumer.consume()

    def test_async_produce_thread_exception(self):
        """Ensure that an exception on a worker thread is raised to the main thread"""
        with self.assertRaises(AttributeError):
            with self._get_producer(min_queued_messages=1) as producer:
                # get some dummy data into the queue that will cause a crash
                # when flushed:
                msg = Message("stuff", partition_id=0)
                del msg.value
                producer._produce(msg)
        while self.consumer.consume() is not None:
            time.sleep(.05)

    def test_required_acks(self):
        """Test with non-default values for `required_acks`

        See #278 for a related bug.  Here, we only test that no exceptions
        occur (hence `sync=True`, which would surface most exceptions)
        """
        kwargs = dict(linger_ms=1, sync=True, required_acks=0)
        prod = self._get_producer(**kwargs)
        prod.produce(uuid4().bytes)

        kwargs["required_acks"] = -1
        prod = self._get_producer(**kwargs)
        prod.produce(uuid4().bytes)

    def test_null_payloads(self):
        """Test that None is accepted as a null payload"""
        prod = self._get_producer(sync=True, min_queued_messages=1)
        prod.produce(None)
        self.assertIsNone(self.consumer.consume().value)
        prod.produce(None, partition_key=b"whatever")
        self.assertIsNone(self.consumer.consume().value)
        prod.produce(b"")  # empty string should be distinguished from None
        self.assertEqual(b"", self.consumer.consume().value)

    def test_owned_broker_flush_message_larger_then_max_request_size(self):
        """Test that producer batches messages into the batches no larger then
        `max_request_size`
        """
        large_payload = b''.join([uuid4().bytes for i in range(50000)])

        producer = self._get_producer(
            auto_start=False
        )
        # setup producer but do not start actually sending messages
        partition = producer._topic.partitions[0]
        owned_broker = OwnedBroker(producer, partition.leader, auto_start=False)

        msg = Message(large_payload, partition_id=0)
        owned_broker.enqueue(msg)

        max_request_size = 1000
        assert max_request_size < len(msg.value)
        with self.assertRaises(MessageSizeTooLarge):
            owned_broker.flush(0, max_request_size)

    def test_owned_broker_flush_batching_by_max_request_size(self):
        """Test that producer batches messages into the batches no larger then
        `max_request_size`
        """
        large_payload = b''.join([uuid4().bytes for i in range(5000)])

        producer = self._get_producer(
            auto_start=False
        )
        # setup producer but do not start actually sending messages
        partition = producer._topic.partitions[0]
        owned_broker = OwnedBroker(producer, partition.leader, auto_start=False)

        for i in range(100):
            msg = Message(large_payload, partition_id=0)
            owned_broker.enqueue(msg)

        batch = owned_broker.flush(0, producer._max_request_size)
        assert len(batch) < 100
        assert sum([len(m.value) for m in batch]) < producer._max_request_size

        # iterate through the rest of the batches and test the same invariant
        while batch:
            batch = owned_broker.flush(0, producer._max_request_size)
            assert len(batch) < 100
            assert sum([len(m.value) for m in batch]) < producer._max_request_size

    def test_async_produce_compression_large_message(self):
        large_payload = b''.join([uuid4().bytes for i in range(50000)])
        assert sys.getsizeof(large_payload) / 1024 / 1024 < 1.0

        prod = self._get_producer(
                compression=CompressionType.SNAPPY,
                delivery_reports=True
                )
        prod.produce(large_payload)

        report = prod.get_delivery_report()
        self.assertEqual(report[0].value, large_payload)
        self.assertIsNone(report[1])

        message = self.consumer.consume()
        assert message.value == large_payload

        for i in range(10):
            prod.produce(large_payload)
        prod._wait_all()

        report = prod.get_delivery_report()
        self.assertEqual(report[0].value, large_payload)
        self.assertIsNone(report[1])
        print("FINISHED")

        # clenaup and consumer all messages
        msgs = []
        def ensure_all_messages_consumed():
            msg = self.consumer.consume()
            if msg:
                msgs.append(msg)
            assert len(msgs) == 10
        retry(ensure_all_messages_consumed)

    def test_async_produce_large_message(self):
        large_payload = b''.join([uuid4().bytes for i in range(50000)])
        assert sys.getsizeof(large_payload) / 1024 / 1024 < 1.0

        prod = self._get_producer(delivery_reports=True)
        prod.produce(large_payload)

        report = prod.get_delivery_report()
        self.assertEqual(report[0].value, large_payload)
        self.assertIsNone(report[1])

        message = self.consumer.consume()
        assert message.value == large_payload

        for i in range(10):
            prod.produce(large_payload)
        prod._wait_all()
        print("FINISHED")

        report = prod.get_delivery_report()
        self.assertEqual(report[0].value, large_payload)
        self.assertIsNone(report[1])

        # clenaup and consumer all messages
        msgs = []
        def ensure_all_messages_consumed():
            msg = self.consumer.consume()
            if msg:
                msgs.append(msg)
            assert len(msgs) == 10
        retry(ensure_all_messages_consumed)



# @pytest.mark.skipif(platform.python_implementation() == "PyPy",
                    # reason="Unresolved crashes")
# class TestGEventProducer(ProducerIntegrationTests):
    # USE_GEVENT = True


if __name__ == "__main__":
    unittest2.main()
