"""Unit tests for utils.notifier.Notifier, in particular that
start_worker()/stop_worker() actually drain the message queue (this was the
bug where Telegram alerts were silently queued and never sent because
start_worker() was never called from main.py). No real network calls are
made — httpx.AsyncClient is mocked.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from utils.notifier import Notifier


def test_notifier_disabled_without_token():
    notifier = Notifier(token='', chat_id='123')
    assert notifier.enabled is False


def test_notifier_disabled_with_placeholder_token():
    notifier = Notifier(token='YOUR_TELEGRAM_BOT_TOKEN', chat_id='123')
    assert notifier.enabled is False


async def test_send_message_is_a_noop_when_disabled():
    notifier = Notifier(token='', chat_id='')
    # Must not raise, and must not create a queue/worker.
    await notifier.send_message("hello")
    assert notifier.enabled is False


def _mock_http_client(status_code=200):
    """Build a mock that satisfies `async with httpx.AsyncClient() as client`."""
    response = AsyncMock()
    response.status_code = status_code
    response.text = ''

    client = AsyncMock()
    client.post.return_value = response

    context_manager = AsyncMock()
    context_manager.__aenter__.return_value = client
    context_manager.__aexit__.return_value = None

    # httpx.AsyncClient(...) itself is a plain (synchronous) constructor call;
    # only entering/exiting the "async with" block is async.
    client_cls = MagicMock(return_value=context_manager)
    return client_cls, client


async def test_start_worker_actually_sends_queued_messages():
    notifier = Notifier(token='FAKE_TOKEN', chat_id='12345')
    assert notifier.enabled is True

    client_cls, client = _mock_http_client()
    with patch('httpx.AsyncClient', client_cls):
        await notifier.start_worker()
        try:
            await notifier.send_message("test message")
            await notifier.message_queue.join()

            client.post.assert_awaited_once()
            _, kwargs = client.post.call_args
            assert kwargs['data']['text'] == 'test message'
            assert kwargs['data']['chat_id'] == '12345'
        finally:
            await notifier.stop_worker()


async def test_send_message_without_start_worker_is_never_delivered():
    """Regression test for the original bug: without start_worker(), messages
    pile up in the queue forever and nothing is ever POSTed to Telegram."""
    notifier = Notifier(token='FAKE_TOKEN', chat_id='12345')

    client_cls, client = _mock_http_client()
    with patch('httpx.AsyncClient', client_cls):
        await notifier.send_message("never sent")
        assert notifier.message_queue.qsize() == 1
        client.post.assert_not_called()


async def test_start_worker_is_idempotent():
    notifier = Notifier(token='FAKE_TOKEN', chat_id='12345')
    client_cls, _ = _mock_http_client()
    with patch('httpx.AsyncClient', client_cls):
        await notifier.start_worker()
        first_task = notifier.worker_task
        await notifier.start_worker()
        assert notifier.worker_task is first_task
        await notifier.stop_worker()
