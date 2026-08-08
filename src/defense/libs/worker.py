import asyncio
import os
import signal
import structlog
from abc import ABC, abstractmethod
from typing import Any, Optional

import redis.asyncio as aioredis
from defense.libs import streams
from defense.libs.dlq import record_failure
from defense.libs.common.config import get_settings

log = structlog.get_logger(__name__)

class StreamWorker(ABC):
    """Abstract base class for Redis Stream workers."""
    
    def __init__(self, consumer_name: str, input_stream: str, consumer_group: str, output_stream: Optional[str] = None):
        self.config = get_settings()
        self.consumer_name = consumer_name
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.consumer_group = consumer_group
        
        self.redis = aioredis.from_url(self.config.redis_url, decode_responses=True)
        self.shutdown_event = asyncio.Event()
        self._setup_signals()

    def _setup_signals(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._signal_handler, sig)

    def _signal_handler(self, sig):
        log.info(f"Received signal {sig.name}, shutting down gracefully...")
        self.shutdown_event.set()

    async def setup(self):
        """Override to run any setup logic like loading models."""
        pass

    async def teardown(self):
        """Override to run any cleanup logic."""
        pass

    @abstractmethod
    async def process_message(self, message_id: str, payload: dict) -> Optional[dict]:
        """Process a single message. Return output payload if any."""
        pass

    async def get_batch_size(self) -> int:
        return 1
        
    async def get_max_retries(self) -> int:
        return 3

    async def run(self):
        log.info(f"Starting {self.__class__.__name__} [{self.consumer_name}]")
        
        # Ensure consumer group exists
        try:
            await self.redis.xgroup_create(self.input_stream, self.consumer_group, mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                log.error("Failed to create consumer group", error=str(e))
                raise
                
        await self.setup()
        
        batch_size = await self.get_batch_size()
        max_retries = await self.get_max_retries()
        
        try:
            while not self.shutdown_event.is_set():
                try:
                    # Try reading PEL first (unacked messages)
                    response = await self.redis.xreadgroup(
                        self.consumer_group,
                        self.consumer_name,
                        {self.input_stream: "0"},
                        count=batch_size,
                        block=0
                    )
                    
                    if not response or not response[0][1]:
                        # Read new messages
                        response = await self.redis.xreadgroup(
                            self.consumer_group,
                            self.consumer_name,
                            {self.input_stream: ">"},
                            count=batch_size,
                            block=self.config.redis_block_ms
                        )
                    
                    if not response:
                        continue
                        
                    for stream_name, messages in response:
                        for msg_id, payload in messages:
                            if self.shutdown_event.is_set():
                                break
                                
                            try:
                                result = await self.process_message(msg_id, payload)
                                
                                if result and self.output_stream:
                                    await self.redis.xadd(self.output_stream, result)
                                    
                                await self.redis.xack(self.input_stream, self.consumer_group, msg_id)
                                
                            except Exception as e:
                                log.error("Error processing message", msg_id=msg_id, error=str(e), exc_info=True)
                                # Increment retries
                                retry_key = f"retry:{self.consumer_group}:{msg_id}"
                                retries = await self.redis.incr(retry_key)
                                
                                if retries > max_retries:
                                    log.warning("Max retries exceeded, sending to DLQ", msg_id=msg_id)
                                    await record_failure(
                                        redis=self.redis,
                                        queue=self.input_stream,
                                        msg_id=msg_id,
                                        payload=payload,
                                        error=e
                                    )
                                    await self.redis.xack(self.input_stream, self.consumer_group, msg_id)
                                    await self.redis.delete(retry_key)

                except asyncio.TimeoutError:
                    continue
                except aioredis.ConnectionError:
                    log.warning("Redis connection error, reconnecting...")
                    await asyncio.sleep(1)
                except Exception as e:
                    log.error("Consumer loop error", error=str(e), exc_info=True)
                    await asyncio.sleep(1)
        finally:
            log.info("Cleaning up...")
            await self.teardown()
            await self.redis.close()
