"""
notification.py
===============
Helper functions for triggering webhook calls.
"""

# Standard python includes
import json
from typing import Any
import aiohttp
import daiquiri
import json
import asyncio
import traceback
from .helper import loop
import common.config as config

# App-specific includes
from common.constants import (
    mercure_events,
)


# Create local logger instance
logger = config.get_logger()


def post(url: str, payload: Any) -> None:
    async def do_post(url, payload) -> None:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        logger.warning(f"Webhook notification failed {url}, status: {resp.status}")
                    # logger.warning(f"{await resp.text()}")
            except Exception as e:
                logger.warning(f"Webhook notification failed {url}, exception: {e}")
                logger.warning(traceback.format_exc())

    asyncio.ensure_future(do_post(url, payload), loop=loop)


def send_webhook(url, payload, event, rule_name, task_id="") -> None:
    if (not url):
        return

    # Replace macros in payload
    payload_parsed = payload

    payload_parsed = payload_parsed.replace("@rule@", rule_name)
    payload_parsed = payload_parsed.replace("@task_id@", task_id)

    if event == mercure_events.RECEPTION:
        payload_parsed = payload_parsed.replace("@event@", "RECEIVED")
    if event == mercure_events.COMPLETION:
        payload_parsed = payload_parsed.replace("@event@", "COMPLETED")
    if event == mercure_events.ERROR:
        payload_parsed = payload_parsed.replace("@event@", "ERROR")

    try:
        payload_data = json.loads("{" + payload_parsed + "}")
        post(url, payload_data)
        # response = requests.post(
        #     url, data=json.dumps(payload_data), headers={"Content-type": "application/json"}
        # )
        # if (response.status_code != 200) and (response.status_code != 204):
        #     logger.error(f"ERROR: Webhook notification failed (status code {response.status_code})")
        #     logger.error(f"ERROR: {response.text}")
    except:
        logger.error(f"ERROR: Webhook notification failed")
        logger.error(traceback.format_exc())
        return
