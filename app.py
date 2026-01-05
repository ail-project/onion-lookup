import json
import re
import requests
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.routing import Route, Mount
from starlette.templating import Jinja2Templates
from starlette.responses import JSONResponse, RedirectResponse
from starlette.datastructures import URL
from starlette.types import ASGIApp, Receive, Scope, Send

repeated_quotes = re.compile(r'//+')  # handling multiple // in url
from urllib.parse import urlparse, unquote
import sys
import uvicorn
import valkey

# disable requests certificate warning
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

templates = Jinja2Templates(directory='templates')

# config
import configparser

config = configparser.ConfigParser()
config.read('config.cfg')
ail_url = config['DEFAULT']['ail_url']
ail_apikey = config['DEFAULT']['ail_apikey']
valkey_host = config['DEFAULT']['valkey_host']
valkey_port = config['DEFAULT']['valkey_port']
cache = valkey.Valkey(host=valkey_host, port=valkey_port, db=0)
# API definition
from apiman.starlette import Apiman

apiman = Apiman(template="openapi.yml")

if not cache.ping():
    sys.exit("Valkey server not reachable")


class HttpUrlRedirectMiddleware:
    """
    This http middleware redirects urls with repeated slashes to the cleaned up
    versions of the urls - from GitHub issue from Starlette project
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:

        if scope["type"] == "http" and repeated_quotes.search(URL(scope=scope).path):
            url = URL(scope=scope)
            url = url.replace(path=repeated_quotes.sub('/', url.path))
            response = RedirectResponse(url, status_code=307)
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def extract_onion_from_url(url):
    words = url.split()
    if len(words) > 1:
        for word in words:
            if '.onion' in word:
                url = word

    if not url.startswith('http'):
        url = f'http://{url}'
    url = unquote(url)
    url = urlparse(url)
    domain = url.netloc.split(':', 1)[0].split('.')
    if len(domain) > 1:
        extracted_onion = f'{domain[-2]}.{domain[-1]}'
        return extracted_onion
    else:
        return None


def stats_onion(onion=None, typeo="global"):
    if onion is None:
        return False
    if typeo == "global":
        r = cache.zincrby("onion-lookup:stats", 1, onion)
    else:
        r = cache.zincrby("onion-lookup:ail-stats", 1, onion)

    return r


def check_onion(onion=None):
    # We only support onion_v3 and automatically append dot onion if missing
    if onion is None or onion == '':
        return False
    if re.match(r'[a-z2-7]{56}.onion', onion, flags=0):
        return onion
    if re.match(r'[a-z2-7]{56}', onion, flags=0):
        return f"{onion}.onion"
    return False


def query_onion(onion=None):
    if onion is None:
        return False
    keycache = f'onion-lookup:{onion}'
    stats_onion(onion=onion)
    if cache.exists(keycache):
        return json.loads(cache.get(keycache))
    headers = {'Authorization': ail_apikey}
    r = requests.get(
        f'{ail_url}/api/v1/lookup/onion/{onion}', headers=headers, verify=False
    )
    if r.status_code != 200:
        return False
    meta = json.loads(r.text)
    # Temporary fix until back-end supports the new taxonomy
    if 'tags' in meta:
        old_tag = 'dark-web:topic="pornography-child-exploitation"'
        new_tag = 'dark-web:topic="child-sexual-abuse-material"'
        meta['tags'] = [new_tag if x == old_tag else x for x in meta['tags']]
        raw_resp = json.dumps(meta)
        cache.set(keycache, raw_resp, ex=3600)
    else:
        cache.set(keycache, r.text, ex=3600)
    stats_onion(onion=onion, typeo="local")
    return json.loads(cache.get(keycache))


async def homepage(request):
    template = "index.html"
    context = {"request": request}
    if 'lookup' in request.query_params:
        onion = extract_onion_from_url(request.query_params['lookup'].lower())
        onion = check_onion(onion=onion)
        if onion is not False:
            context['onion'] = onion
            onion_meta = query_onion(onion=onion)
            if onion_meta is not False:
                context['onion_meta'] = onion_meta
        else:
            context['error'] = 'Incorrect format'

    return templates.TemplateResponse(template, context)


@apiman.from_yaml(
    """
    summary: lookup api
    tags:
    - lookup
    parameters:
    - name: onion 
      in: path 
      required: True
      schema:
        type: string
    responses:
      "200":
        description: OK
    """
)
async def lookup(request):
    onion = extract_onion_from_url(request.path_params['onion'].lower())
    onion = check_onion(onion=onion)
    keycache = f'onion-lookup:{onion}'
    if onion:
        onion_response = check_onion(onion=onion)
        if onion_response is not False:
            onion_meta = query_onion(onion=onion)
            if onion_meta is not False:
                return JSONResponse(onion_meta)
        else:
            return JSONResponse({"error": "Incorrect onion format"})
    return JSONResponse({})


async def error(request):
    """
    Generic catch-call error
    """
    raise RuntimeError("Oh no")


async def not_found(request: Request, exc: HTTPException):
    """
    Return an HTTP 404 page.
    """
    template = "404.html"
    context = {"request": request}
    return templates.TemplateResponse(template, context, status_code=404)


async def server_error(request: Request, exc: HTTPException):
    """
    Return an HTTP 500 page.
    """
    template = "500.html"
    context = {"request": request}
    return templates.TemplateResponse(template, context, status_code=500)


routes = [
    Route('/', homepage),
    Route('/api/lookup/{onion}', lookup, methods=['GET']),
    Route('/error', error),
    Mount('/static', app=StaticFiles(directory='statics'), name='static'),
]

exception_handlers = {404: not_found, 500: server_error}

app = Starlette(debug=False, routes=routes, exception_handlers=exception_handlers)
app.add_middleware(HttpUrlRedirectMiddleware)
apiman.init_app(app)

if __name__ == "__main__":
    uvicorn.run(app, host='0.0.0.0', port=8000)
