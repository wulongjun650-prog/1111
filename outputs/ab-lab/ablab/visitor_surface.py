"""Names, paths, and cookies that may reach browsers or crawlers.

Legacy values stay listed so older published assets and cookies keep working.
"""

CONTACT_BRIDGE_SCRIPT_ID = 'pg-contact-bridge'
NUMBER_POOL_SCRIPT_ID = 'pg-number-pool'

LEGACY_CONTACT_BRIDGE_SCRIPT_ID = 'ab-dest-app'
LEGACY_NUMBER_POOL_SCRIPT_ID = 'ab-number-split'

HUMAN_CLICK_SCRIPT_ID = 'pg-click-guard'
HUMAN_BROWSE_DIV_ID = 'pg-verify-slot'
HUMAN_BROWSE_STYLE_ID = 'pg-verify-style'
HUMAN_BROWSE_LOADER_ID = 'pg-verify-loader'
HUMAN_BROWSE_JS_NAME = 'pg-verify.js'
HUMAN_BROWSE_JS_MARK = b'(function(){'

LEGACY_HUMAN_CLICK_SCRIPT_ID = 'ab-human-click'
LEGACY_HUMAN_BROWSE_STYLE_ID = 'ab-human-browse-style'
LEGACY_HUMAN_BROWSE_DIV_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_LOADER_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_JS_NAME = 'ab-human-browse.js'
LEGACY_HUMAN_BROWSE_JS_MARK = b'/* ab-human-browse */'

# Cloudflare page rules on live zones already match this query flag.
HUMAN_BUTTON_QUERY_FLAG = 'pg_human'
LEGACY_HUMAN_BUTTON_QUERY_FLAG = 'ab_human'

EMPTY_SLOT_TITLE = '内容准备中'
EMPTY_SLOT_DETAIL = '请稍后再访问。'

EVENT_INGEST_PATH = '/api/j'
LEGACY_EVENT_INGEST_PATH = '/wa-events'

CLIENT_EVENT_PATH = '/api/collect'

ROUTE_PROOF_PREFIX = '/.well-known/site-verify/'
LEGACY_ROUTE_PROOF_PREFIX = '/.well-known/ab-lab-route/'

SESSION_COOKIE = '__Host-pg_sess'
LEGACY_SESSION_COOKIE = '__Host-ab_session'

SPLIT_COOKIE = 'pg_split'
LEGACY_SPLIT_COOKIE = 'ab_split'

WA_COOKIE = 'pg_wa'
LEGACY_WA_COOKIE = 'ab_wa'

# Prior admin hosts whose eventEndpoint may still be baked into old HTML.
LEGACY_ADMIN_ORIGINS = (
    'https://hhucuq.top',
    'http://hhucuq.top',
)

DEFAULT_ADMIN_ORIGIN = 'https://baozha98.sbs'
