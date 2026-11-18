"""couchd/youtube.js, run in Node with just enough of a browser around it."""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "couchd" / "youtube.js"

# A browser's globals, as far as youtube.js touches them. open() and fetch() record the URL
# they were given; the script then wraps them.
HARNESS = """
const seen = [];
globalThis.window = globalThis.top = globalThis;
globalThis.location = new URL("https://www.youtube.com/tv#/");
globalThis.HTMLMediaElement = class { canPlayType() { return "probably"; } };
globalThis.XMLHttpRequest = class { open(method, url) { seen.push(String(url)); } };
globalThis.fetch = (input) => { seen.push(String(input)); return Promise.resolve(); };
globalThis.addEventListener = () => {};
eval(require("fs").readFileSync(process.argv[1], "utf8"));
for (const url of JSON.parse(process.argv[2])) {
  new XMLHttpRequest().open("GET", url);
  fetch(url);
}
console.log(JSON.stringify(seen));
"""

BIND = (
    "https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&id=abc"
    "&name=YouTube%20on%20TV&app=lb-v4&theme=cl&VER=8&v=2&RID=1"
    "&deviceInfo=" + json.dumps({"brand": "Samsung", "model": "SmartTV", "os": "Tizen"})
)


def run(urls: list[str]) -> list[str]:
    if shutil.which("node") is None:
        pytest.skip("needs node")
    out = subprocess.run(
        ["node", "-e", HARNESS, str(SCRIPT), json.dumps(urls)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


def test_phones_are_told_the_tv_is_called_couch():
    for url in run([BIND]):  # through XMLHttpRequest and through fetch
        query = parse_qs(urlsplit(url).query)
        assert query["name"] == ["Couch"]
        device = json.loads(query["deviceInfo"][0])
        assert device == {"brand": "Couch", "model": "Couch", "os": "Tizen"}
        assert query["device"] == ["LOUNGE_SCREEN"] and query["RID"] == ["1"]


def test_only_the_lounge_is_told():
    other = "https://www.youtube.com/youtubei/v1/browse?name=YouTube%20on%20TV"
    assert run([other]) == [other, other]
