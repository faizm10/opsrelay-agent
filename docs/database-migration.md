# Move an OpsRelay MongoDB database

Use one offline dump per application database. The MongoDB path supports a runtime cutover. The PostgreSQL path loads a private staging schema for querying or subsequent relational mapping. OpsRelay still requires its existing storage backend to run.

## Take the dump

Install the MongoDB Database Tools. Pause writers, including scheduled checks, agent jobs and webhook ingestion, before taking the dump and keep them paused through a MongoDB cutover. Account for TTL expiry when comparing counts. Record the source collection names, document counts and index definitions. Provision project users, network access and any Atlas Search or vector indexes separately.

Store the source URI in an owner-readable tools config file outside the repo, with a single YAML field named `uri`. Use a read-only database user. Choose a new dump directory on protected storage. The example uses the development database name. Set it to the database you intend to migrate.

```sh
umask 077
DB_NAME=opsrelay_dev
DUMP_DIR=$(mktemp -d)
mongodump --config="$SOURCE_CONFIG" --db="$DB_NAME" --out="$DUMP_DIR"
```

Continue only after a successful dump. Keep its files together.

## Restore into another MongoDB project

Use a separate tools config file for the destination cluster. Verify the project and confirm the database of the same name is empty. Use compatible MongoDB versions and restore into an unused database so failed restores can be retried against a fresh destination.

```sh
mongorestore --config="$DESTINATION_CONFIG" --nsInclude="$DB_NAME.*" --stopOnError "$DUMP_DIR"
```

Check the restore exit status, compare collection counts and indexes, and read a known incident by ID. Point all OpsRelay readers and writers at the new project together, keeping the database name and `OPSRELAY_SECRET_KEY` consistent. Check dashboard history and connector decryption before resuming writers. Before new writes, rollback consists of restoring the original connection settings. After new writes, reconcile the two databases before switching back. [MongoDB restore reference](https://www.mongodb.com/docs/database-tools/mongorestore/)

## Import into Supabase or Neon

Set `DESTINATION_DATABASE_URL` in your shell to the destination Postgres connection string with TLS enabled. Use a direct connection, or Supabase's session pooler for IPv4. The importing role needs permission to create a schema. Leave `opsrelay_import` outside Supabase's exposed schemas. [Supabase connections](https://supabase.com/docs/guides/database/connecting-to-postgres) · [Neon connections](https://neon.com/docs/connect/connect-from-any-app)

From the repository root, run the importer with uv. Pass the database subdirectory containing the uncompressed BSON and metadata files for ordinary collections.

```sh
uv run --script scripts/import_mongo_postgres.py "$DUMP_DIR/$DB_NAME"
```

The importer streams documents into one transaction. An error rolls back the import. A second run against a committed import stops at schema creation. Each document has its original BSON bytes and a queryable canonical Extended JSON representation. Collection options and index definitions are stored as metadata. PostgreSQL constraints, application tables and TTL jobs require separate mapping.

Compare these results with the source inventory, including empty collections, before using the copy.

```sql
SELECT c.name, c.documents AS imported, count(d.collection) AS stored
FROM opsrelay_import.collections c
LEFT JOIN opsrelay_import.documents d ON d.collection = c.name
GROUP BY c.name, c.documents
ORDER BY c.name;
```
