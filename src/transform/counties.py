"""Counties shown on the site, north to south (the order of the county switcher).

code      member prefix in Taipower's ZIPs (CHANGHUA_capacity.csv, ...)
moi       MOI county names the file covers; CHIAYI covers county and city
source    archive the county's own tables come from. S02 is Taipower's Yunlin-only
          file, kept for Yunlin because it has one more archived version; S01 is the
          national file, whose Yunlin tables are byte-identical to S02 on every common date.
district  Taipower district office (區營業處) in the S04 waiting-queue table; each of
          these serves exactly the area of one county file.
"""

COUNTIES = [
    {"code": "CHANGHUA", "slug": "changhua", "name": "彰化縣", "short": "彰化", "moi": ["彰化縣"], "source": "S01", "district": "彰化"},
    {"code": "YUNLIN", "slug": "yunlin", "name": "雲林縣", "short": "雲林", "moi": ["雲林縣"], "source": "S02", "district": "雲林"},
    {"code": "CHIAYI", "slug": "chiayi", "name": "嘉義縣市", "short": "嘉義", "moi": ["嘉義縣", "嘉義市"], "source": "S01", "district": "嘉義"},
]
# The site started with Yunlin only; pages without ?county= keep showing it so old links still work.
DEFAULT_COUNTY = "yunlin"
