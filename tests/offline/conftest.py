"""Hard isolation for mocked tests: no database or network access is permitted."""

import socket

import pymysql
import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_external_access(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline test attempted real network or database access")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(pymysql, "connect", blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)
