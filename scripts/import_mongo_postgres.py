# /// script
# requires-python = ">=3.10,<3.15"
# dependencies = ["pymongo==4.19.0", "psycopg[binary]==3.2.13"]
# ///
import argparse
import json
import os
from pathlib import Path

import bson
import psycopg
from bson import json_util
from bson.codec_options import CodecOptions, DatetimeConversion
from bson.raw_bson import RawBSONDocument

CODEC = CodecOptions(document_class=RawBSONDocument, datetime_conversion=DatetimeConversion.DATETIME_AUTO)


def import_dump(directory, connection):
    files = sorted(directory.glob("*.bson"))
    metadata = set(directory.glob("*.metadata.json"))
    unexpected = set(directory.iterdir()) - set(files) - metadata - {directory / "prelude.json"}
    if unexpected or not files or metadata != {path.with_suffix(".metadata.json") for path in files}:
        raise ValueError("Expected one uncompressed mongodump database directory with ordinary collections.")
    with connection.cursor() as cur:
        cur.execute("CREATE SCHEMA opsrelay_import")
        cur.execute("REVOKE ALL ON SCHEMA opsrelay_import FROM PUBLIC")
        cur.execute("""CREATE TABLE opsrelay_import.collections (
            name text PRIMARY KEY, metadata json NOT NULL, documents bigint NOT NULL
        )""")
        cur.execute("""CREATE TABLE opsrelay_import.documents (
            collection text NOT NULL REFERENCES opsrelay_import.collections,
            document json NOT NULL, raw_bson bytea NOT NULL
        )""")
        for table in ("collections", "documents"):
            cur.execute(f"ALTER TABLE opsrelay_import.{table} ENABLE ROW LEVEL SECURITY")
        for path in files:
            info = json.loads(path.with_suffix(".metadata.json").read_text())
            name = info["collectionName"]
            if info.get("type", "collection") != "collection" or "timeseries" in info.get("options", {}):
                raise ValueError(f"Unsupported collection type for {name}")
            cur.execute("INSERT INTO opsrelay_import.collections VALUES (%s, %s, 0)", (name, json.dumps(info)))
            count = 0
            with path.open("rb") as stream, cur.copy("COPY opsrelay_import.documents FROM STDIN") as copy:
                for doc in bson.decode_file_iter(stream, codec_options=CODEC):
                    copy.write_row((name, json_util.dumps(doc, json_options=json_util.CANONICAL_JSON_OPTIONS), doc.raw))
                    count += 1
            cur.execute("UPDATE opsrelay_import.collections SET documents = %s WHERE name = %s", (count, name))
            print(f"{name}: {count} documents staged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import a MongoDB database dump into a fresh PostgreSQL schema.")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    with psycopg.connect(os.environ["DESTINATION_DATABASE_URL"]) as conn:
        import_dump(args.directory, conn)
    print("Committed opsrelay_import.")
