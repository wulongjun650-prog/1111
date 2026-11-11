"""Names and markers that may appear in HTML served to visitors.

Keep these free of product-specific prefixes. Legacy strings remain for
detecting older published pages when re-stamping or injecting.
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
HUMAN_BROWSE_JS_MARK = b'/* pg-verify */'

LEGACY_HUMAN_CLICK_SCRIPT_ID = 'ab-human-click'
LEGACY_HUMAN_BROWSE_STYLE_ID = 'ab-human-browse-style'
LEGACY_HUMAN_BROWSE_DIV_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_LOADER_ID = 'ab-human-browse'
LEGACY_HUMAN_BROWSE_JS_NAME = 'ab-human-browse.js'
LEGACY_HUMAN_BROWSE_JS_MARK = b'/* ab-human-browse */'

# Cloudflare page rules on live zones already match this query flag.
HUMAN_BUTTON_QUERY_FLAG = 'ab_human'

EMPTY_SLOT_TITLE = '内容准备中'
EMPTY_SLOT_DETAIL = '请稍后再访问。'
