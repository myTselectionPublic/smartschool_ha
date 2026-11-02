import json
import logging
import hashlib
from pathlib import Path
import pprint
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List
import requests
from pydantic import BaseModel
from enum import Enum
import re
import urllib.parse
from ratelimit import limits, sleep_and_retry

import voluptuous as vol
from custom_components.smartschool.smartschool_api.planner import ApplicableAssignmentTypes, Planner
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import (
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

def check_settings(config, hass):
    if not any(config.get(i) for i in ["username"]):
        _LOGGER.error("username was not set")
    else:
        return True
    if not config.get("password"):
        _LOGGER.error("password was not set")
    else:
        return True

    raise vol.Invalid("Missing settings to setup the sensor.")
    

from .smartschool_api import (
    Smartschool,
    AppCredentials,
    FutureTasks,
    SmartschoolLessons, 
    Results,
    MessageHeaders,
    MarkMessageRead
)

class ComponentSession(object):
    def __init__(self, cookie_directory=None):
        self.smartschool = None
        self.creds = None
        self._cookie_directory = Path(cookie_directory) if cookie_directory else None

    
    @sleep_and_retry
    @limits(calls=1, period=5)
    def login(self, username, password, smartschool_domain, mfa):
        creds = AppCredentials(username, password, smartschool_domain, mfa)
        creds.validate()
        if self.smartschool is not None and creds == self.creds:
            _LOGGER.debug("Reusing existing Smartschool session")
            return {}

        _LOGGER.info("Creating Smartschool session")
        cookie_file = None
        if self._cookie_directory is not None:
            account = json.dumps([creds.main_url.lower(), creds.username])
            account_key = hashlib.sha256(account.encode("utf-8")).hexdigest()
            cookie_file = self._cookie_directory / f"{account_key}.cookies"
        smartschool = Smartschool(creds=creds, cookie_file=cookie_file)
        if self.smartschool is not None:
            self.smartschool._session.close()
        self.creds = creds
        self.smartschool = smartschool
        return {}

    @sleep_and_retry
    @limits(calls=1, period=5)
    def getFutureTasks(self):
        return FutureTasks(smartschool=self.smartschool)

    @sleep_and_retry
    @limits(calls=1, period=5)
    def getAgenda(self, timestamp_to_use: datetime | None = None):
        agenda = list(SmartschoolLessons(smartschool=self.smartschool, timestamp_to_use=timestamp_to_use))
        return agenda

    @sleep_and_retry
    @limits(calls=1, period=5)
    def getResults(self):
        results = []
        try:
            results = list(Results(smartschool=self.smartschool))
        except Exception as e:
            _LOGGER.error(e)
        return results
    
    @sleep_and_retry
    @limits(calls=1, period=5)
    def getMessages(self, timestamp_to_use: datetime | None = None):
        agenda = list(MessageHeaders(smartschool=self.smartschool))
        return agenda

    @sleep_and_retry
    @limits(calls=1, period=5)
    def markAllUnreadMessagesRead(self):
        messages = self.getMessages()
        unread_messages = [message for message in messages if message.status == 0]
        for message in unread_messages:
            list(MarkMessageRead(smartschool=self.smartschool, msg_id=message.id))

        return len(unread_messages)
    
    
    @sleep_and_retry
    @limits(calls=1, period=5)
    def getPlanner(self, from_date: datetime | None = None, till_date: datetime | None = None, planner_type: str | None = None):
        results = list(Planner(smartschool=self.smartschool, from_date=from_date, till_date=till_date, planner_type=planner_type))
        return results
    
    
    @sleep_and_retry
    @limits(calls=1, period=5)
    def getApplicableAssignmentTypes(self):
        results = list(ApplicableAssignmentTypes(smartschool=self.smartschool))
        return results
