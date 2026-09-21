# Bundled country database

IP Geolocation by [DB-IP](https://db-ip.com).

`country.mmdb`: DB-IP Country Lite, September 2026, downloaded from
https://download.db-ip.com/free/dbip-country-lite-2026-09.mmdb.gz

Original uncompressed file (unaltered): 8,340,464 bytes.
MD5: d4c12ea6949d09b6a975084ea405d51c
SHA1: 385d4ab1e08417634a0a64921ac0e9c15c4c5e8a
Both matched the publisher's download page on 2026-09-20.

Licensed under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/).
Source / attribution / license: https://db-ip.com/db/lite.php
License grants no endorsement. Database has not been modified; it is renamed country.mmdb.
Keep attribution on pages using results, including the admin footer.

Lite has reduced coverage and accuracy compared with the commercial database.
The publisher updates it monthly. This app does not silently download updates.
To update, download a newer Country Lite MMDB from the official page, verify the
published checksum, retain this attribution, stop the app, replace the file and
restart. Keep the previous file as a rollback copy. Or set `geoip_path` to your
own licensed Country/City MMDB; an invalid configured path does not silently fall
back to a different database.
