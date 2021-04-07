from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
import random
import re
import requests
import requests_unixsocket
import subprocess
import time

import check50

"""
The user is created in register().
All tests which need to login have to depend on register().
"""
USERNAME = 'check50'
PASSWORD = 'secret_123!'


@check50.check()
def app_exists():
    """app.js exists"""
    check50.exists("app.js")


@check50.check(app_exists)
def env():
    """load environment variables"""
    check50.exists(".env")
    load_dotenv(dotenv_path='.env')
    if not os.getenv("DB_CON_STRING"):
        raise check50.Failure('The file .env does not specify DB_CON_STRING')
    if not os.getenv("API_KEY"):
        raise check50.Failure('The file .env does not specify API_KEY')


@check50.check(env)
def npm_install():
    """install node modules"""
    check50.exists("package.json")
    check50.exists("package-lock.json")
    check50.run("npm install").exit(code=0, timeout=20)
    check50.exists("node_modules")


@check50.check(npm_install)
def startup():
    """application starts up"""
    with App() as app:
        app.get('/').status(200)

@check50.check(startup)
def register_page():
    """register page has all required elements"""
    with App() as app:
        app.get('/register').status(200).css_select([
            'input[name=username]',
            'input[name=password]',
            'input[name=confirmation]',
        ])


@check50.check(register_page)
def register_empty_field():
    """registration with an empty field fails"""
    users = [
        ("", "secret", "secret"),
        ("check50", "secret", ""),
        ("check50", "", "")
    ]
    with App() as app:
        for u in users:
            app.register(*u).status(400)


@check50.check(register_page)
def register_password_mismatch():
    """registration with password mismatch fails"""
    with App() as app:
        app.register("check50", "secret_123!", "secret_999!").status(400)


@check50.check(register_page)
def register():
    """registering user succeeds"""
    user = [
        'check50_' + str(random.randint(10000000, 99999999)),
        'check50_123!',
        'check50_123!',
    ]
    with App() as app:
        app.register(*user).status(200)
        # Register in case the test runs for the first time
        app.register(USERNAME, PASSWORD, PASSWORD)


@check50.check(register)
def register_duplicate_username():
    """registration rejects duplicate username"""
    with App() as app:
        app.register(USERNAME, PASSWORD, PASSWORD).status(400)


@check50.check(startup)
def login_page():
    """login page has all required elements"""
    with App() as app:
        app.get('/login').status(200).css_select([
            'input[name=username]',
            'input[name=password]',
        ])


@check50.check(register)
def login():
    """login as registered user succceeds"""
    with App() as app:
        app.login(USERNAME, PASSWORD).status(200) \
            .get("/", allow_redirects=False).status(200)


@check50.check(login)
def quote_page():
    """quote page has all required elements"""
    with App() as app:
        app.login(USERNAME, PASSWORD) \
            .get('/quote').css_select('input[name=symbol]')


@check50.check(quote_page)
def quote_handles_invalid():
    """quote handles invalid ticker symbol"""
    with App() as app:
        app.login(USERNAME, PASSWORD).quote("ZZZ").status(400)

@check50.check(quote_page)
def quote_handles_blank():
    """quote handles blank ticker symbol"""
    with App() as app:
        app.login(USERNAME, PASSWORD).quote("").status(400)


@check50.check(quote_page)
def quote_handles_valid():
    """quote handles valid ticker symbol"""
    quote = quote_lookup('NFX')

    with App() as app:
        app.login(USERNAME, PASSWORD) \
           .quote('NFX') \
           .status(200) \
           .content(quote['name'], help="Failed to find the quote's name.") \
           .content(quote['price'], help="Failed to find the quote's price.") \
           .content(quote['symbol'], help="Failed to find the quote's symbol.")


def quote_lookup(symbol):
    load_dotenv(dotenv_path='.env')

    url = f'https://cloud-sse.iexapis.com/stable/stock/{symbol}/quote'
    params = {
        'token': os.getenv('API_KEY'),
    }

    r = requests.get(url, params=params)
    data = r.json()

    return {
        'name':   data['companyName'],
        'price':  data['latestPrice'],
        'symbol': data['symbol'],
    }


class App():
    def __init__(self):
        self._session = requests_unixsocket.Session()
        self._response = None
        self._proc = None

    def __enter__(self):
        """
        We need to close the socket in case of an exception.
        Use Context Manager.
        """

        # check50 starts each different checks in different processes.
        # We need to reload the environment variables in each check.
        load_dotenv(dotenv_path='.env')

        cmd = ['node', 'app.js']
        # Bind the app to a UNIX domain socket to run checks in parallel.
        env = { **os.environ, 'PORT': 'app.sock' }
        self._proc = subprocess.Popen(cmd, env=env)

        # Wait up to 10 seconds for the server to startup.
        for i in range(0,10):
            if self._proc.poll():
                raise check50.Failure(
                        f'Server crashed with code {self._proc.returncode}')
            if os.path.exists('app.sock'):
                break
            time.sleep(1)
        else:
            raise check50.Failure('Server not started within 10 seconds')
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._proc.terminate()
        self._proc.wait(timeout=5)
        os.remove('app.sock')

    def _send(self, method, route, **kwargs):
        prefix = 'http+unix://app.sock'
        url = prefix + route

        """
        We need to prefix redirect urls like '/' or '/login'.
        Therefore disable redirects and follow them manually.
        """
        follow_redirects = kwargs.get('allow_redirects', True)
        kwargs.setdefault('allow_redirects', False)

        try:
            self._response = self._session.request(method=method, url=url,
                **kwargs)

            if not follow_redirects:
                return

            redirects = 0
            while self._response.is_redirect and redirects < 3:
                redirects += 1
                req = self._response.next

                if req.url.startswith('/'):
                    req.url = prefix + self._response.next.url

                """Hack: Manually set cookies"""
                req.prepare_cookies(self._session.cookies)
                self._response = self._session.send(req)
        except requests.exceptions.ConnectionError:
            raise check50.Failure('Server Connection failed.',
                help='Maybe the Server did not start')

    def get(self, route, **kwargs):
        self._send('get', route, **kwargs)
        return self

    def post(self, route, **kwargs):
        self._send('post', route, **kwargs)
        return self

    def status(self, code):
        if (self._response.status_code != code):
            raise check50.Failure(f'expected status code {code} but got ' +
                f'{self._response.status_code}')
        return self

    def css_select(self, selectors):
        if not isinstance(selectors, list):
            selectors = [selectors]

        soup = BeautifulSoup(self._response.content)

        missing = []
        for s in selectors:
            if not soup.select_one(s):
                missing.append(s)

        if missing:
            raise check50.Failure('expect to find html elements matching ' +
                    ', '.join(missing))
        return self

    def content(self, regex, help=None):
        if help is None:
            help = f'expected to find {regex}'

        text = BeautifulSoup(self._response.content).get_text()

        regxp = re.compile(str(regex))
        if not regxp.search(text):
            raise check50.Failure(help)

        return self

    def register(self, username, password, confirmation):
        data = {
            'username': username,
            'password': password,
            'confirmation': confirmation,
        }
        self.post('/register', data=data)
        return self

    def login(self, username, password):
        data = {
            'username': username,
            'password': password,
        }
        self.post('/login', data=data)
        return self

    def quote(self, symbol):
        data = {
            'symbol': symbol,
        }
        self.post('/quote', data=data)
        return self
