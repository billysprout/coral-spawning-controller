# data/

The controller's working folder, mounted at `/data` in the containers. It
starts empty. On the first start (`sh scripts/start.sh`, or the tank container
itself) `scripts/cue_import.py ensure` downloads the reef cue tables from
https://github.com/realfakedoors-idaho/reef-cue-tables (settings `CUE_TABLES_URL`, `CUE_TABLES_REF`, `CUE_TABLES_AUTO_IMPORT`
in `.env`) and installs them here: `locales/`, `external/oisst_locales/`,
`reference/` (the Okinawa reference parameters), the reference calendar, the
arc catalog and `data_manifest.json`. `cue_tables.json` records what was
installed. Everything else here (logs, sensor readings, plug state, your
`tank_config.json`) is written by your own tank and is gitignored.
