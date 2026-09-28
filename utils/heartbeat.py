# utils/heartbeat.py
import asyncio
import logging
import os
import time


class Heartbeat:
    """Periodically touches a file so an external healthcheck (e.g. Docker's
    HEALTHCHECK) can tell whether the main loop is actually still alive, as
    opposed to the process being up but wedged (e.g. every websocket dead
    but the event loop still spinning)."""

    def __init__(self, path: str = 'logs/heartbeat', interval_s: float = 15.0):
        self.path = path
        self.interval_s = interval_s
        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        while True:
            try:
                with open(self.path, 'w') as f:
                    f.write(str(time.time()))
            except Exception as e:
                self.logger.error(f"Failed to write heartbeat file: {e}")
            await asyncio.sleep(self.interval_s)
