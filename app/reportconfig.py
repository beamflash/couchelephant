"""Where issue reports go. The constants are the owner's Firestore project and
the public half of the sealing key; both are safe to ship. Empty until filled,
and while empty nothing is sent."""
import base64
import os
from dataclasses import dataclass

from . import sealing

PROJECT = ""
API_KEY = ""
PUBLIC_KEY_B64 = ""
KEY_ID = ""
DEFAULT_URL = "https://firestore.googleapis.com"


@dataclass(frozen=True)
class ReportConfig:
    public_key: bytes
    key_id: str
    project: str
    api_key: str
    base_url: str


def config() -> ReportConfig | None:
    env = os.environ.get
    pub_b64 = env("COUCHELEPHANT_REPORT_PUBLIC_KEY") or PUBLIC_KEY_B64
    project = env("COUCHELEPHANT_REPORT_PROJECT") or PROJECT
    api_key = env("COUCHELEPHANT_REPORT_API_KEY") or API_KEY
    if not (pub_b64 and project and api_key):
        return None
    try:
        public = base64.b64decode(pub_b64, validate=True)
    except ValueError:
        return None
    if len(public) != 32:
        return None
    # The id is only a label for which key sealed it; derive it when unset.
    key_id = KEY_ID or sealing.key_id(public)
    base = env("COUCHELEPHANT_REPORT_FIRESTORE_URL") or DEFAULT_URL
    return ReportConfig(public, key_id, project, api_key, base.rstrip("/"))
