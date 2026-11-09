import os

import pytest
import requests
from dotenv import dotenv_values

if os.environ.get('KUDCLIP_ALLOW_LEGACY_LIVE_TESTS') != 'isolated':
    pytest.skip('Legacy live tests require an isolated server. Use backend/offline_tests instead.', allow_module_level=True)

frontend_env = dotenv_values("/app/frontend/.env")
base_url = os.environ.get('KUDCLIP_TEST_BASE_URL')
if not base_url:
    raise RuntimeError('KUDCLIP_TEST_BASE_URL must point to an isolated test server')
BASE_URL = base_url.rstrip("/")


@pytest.fixture(scope="session")
def base_url_fixture():
    return BASE_URL


@pytest.fixture(scope="class")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s
