#include "tools.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sqlite3.h>
#include <unistd.h>
#include <zlib.h>

static cJSON *success(Result r) {
  if (r.is_error) fprintf(stderr, "%s\n", r.text);
  assert(!r.is_error);
  cJSON *value = cJSON_Parse(r.text);
  assert(value);
  result_free(r);
  return value;
}
int main(int argc, char **argv) {
  char temporary[] = "/tmp/arxiv-native-test-XXXXXX";
  const char *path = argc == 2 ? argv[1] : temporary;
  if (argc != 2) { int fd = mkstemp(temporary); assert(fd >= 0); close(fd); }
  sqlite3 *db = NULL; assert(sqlite3_open(path, &db) == SQLITE_OK);
  const char *schema =
    "CREATE TABLE papers(id INTEGER PRIMARY KEY,paper_id TEXT UNIQUE,title TEXT,abstract TEXT,category TEXT,license TEXT,sha256 TEXT,text_chars INTEGER,text_bytes INTEGER,body BLOB);"
    "CREATE VIRTUAL TABLE search USING fts5(title,abstract,body,content='',tokenize='porter unicode61');"
    "CREATE TABLE settings(key TEXT,value TEXT);"
    "INSERT INTO settings VALUES('revision','fixture');"
    "CREATE TABLE imports(shard TEXT,rows_done INTEGER,complete INTEGER);"
    "INSERT INTO imports VALUES('fixture',1,1);";
  assert(sqlite3_exec(db, schema, NULL, NULL, NULL) == SQLITE_OK);
  char body[41000] = "αβ😀 rarephysicalterm tail";
  for (int i = 0; i < 4990; ++i) strcat(body, " padding");
  strcat(body, " uniquetailneedle");
  unsigned char compressed[1024]; uLongf length = sizeof compressed;
  assert(compress(compressed, &length, (const Bytef *)body, strlen(body)) == Z_OK);
  sqlite3_stmt *statement;
  assert(sqlite3_prepare_v2(db, "INSERT INTO papers VALUES(1,'old/001','Title','Abstract','physics',NULL,'fixture',?,?,?)", -1, &statement, NULL) == SQLITE_OK);
  sqlite3_bind_int(statement, 1, (int)strlen(body) - 5);
  sqlite3_bind_int(statement, 2, (int)strlen(body));
  sqlite3_bind_blob(statement, 3, compressed, (int)length, SQLITE_TRANSIENT);
  assert(sqlite3_step(statement) == SQLITE_DONE); sqlite3_finalize(statement);
  assert(sqlite3_prepare_v2(db, "INSERT INTO search(rowid,title,abstract,body) VALUES(1,'Title','Abstract',?)", -1, &statement, NULL) == SQLITE_OK);
  sqlite3_bind_text(statement, 1, body, -1, SQLITE_TRANSIENT);
  assert(sqlite3_step(statement) == SQLITE_DONE); sqlite3_finalize(statement);
  sqlite3_close(db);
  setenv("ARXIV_DATABASE", path, 1);

  cJSON *args = cJSON_Parse("{\"paper_id\":\"old/001\",\"offset\":1,\"length\":2}");
  cJSON *out = success(tool_paper(args));
  assert(!strcmp(arg_str(out, "text"), "β😀"));
  assert(arg_int(out, "next_offset", -1) == 3);
  assert(!strcmp(arg_str(out, "url"), "https://arxiv.org/abs/old/001"));
  cJSON_Delete(out); cJSON_Delete(args);
  args = cJSON_Parse("{\"paper_id\":\"old/001\",\"length\":50000}");
  out = success(tool_paper(args)); assert(!strcmp(arg_str(out, "text"), body));
  assert(cJSON_IsNull(cJSON_GetObjectItem(out, "next_offset")));
  cJSON_Delete(out); cJSON_Delete(args);
  args = cJSON_Parse("{\"query\":\"rarephysicalterm tail\",\"category\":\"physics\"}");
  out = success(tool_search(args));
  assert(cJSON_GetArraySize(cJSON_GetObjectItem(out, "papers")) == 1);
  cJSON_Delete(out); cJSON_Delete(args);
  args = cJSON_Parse("{\"query\":\"rarephysicalterm\",\"category\":\"math\"}");
  out = success(tool_search(args));
  assert(cJSON_GetArraySize(cJSON_GetObjectItem(out, "papers")) == 0);
  cJSON_Delete(out); cJSON_Delete(args);
  args = cJSON_Parse("{\"query\":\";\"}");
  Result bad = tool_search(args); assert(bad.is_error); result_free(bad); cJSON_Delete(args);
  args = cJSON_Parse("{\"query\":\"tail\",\"limit\":51}");
  bad = tool_search(args); assert(bad.is_error); result_free(bad); cJSON_Delete(args);
  args = cJSON_Parse("{\"query\":\"uniquetailneedle\"}");
  out = success(tool_search(args));
  assert(cJSON_GetArraySize(cJSON_GetObjectItem(out, "papers")) == 1);
  cJSON_Delete(out); cJSON_Delete(args);
  char rebuilt[41000] = ""; int offset = 0, pages = 0;
  do {
    args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "paper_id", "old/001");
    cJSON_AddNumberToObject(args, "offset", offset);
    cJSON_AddNumberToObject(args, "length", 12000);
    out = success(tool_paper(args));
    strcat(rebuilt, arg_str(out, "text"));
    offset = arg_int(out, "next_offset", -1); ++pages;
    cJSON_Delete(out); cJSON_Delete(args);
  } while (offset >= 0);
  assert(pages == 4 && !strcmp(rebuilt, body));
  out = success(tool_status(NULL)); assert(arg_int(out, "papers", 0) == 1);
  assert(!strcmp(arg_str(out, "count_basis"), "indexed_documents")); cJSON_Delete(out);
  /* Exercise FTS5's maintained count, including empty index, rather than
     a row scan that timed out on the live multi-million-paper NAS corpus. */
  assert(sqlite3_open(path, &db) == SQLITE_OK);
  assert(sqlite3_exec(db, "INSERT INTO search(rowid,title,abstract,body) VALUES(2,'','','')", NULL, NULL, NULL) == SQLITE_OK);
  out = success(tool_status(NULL)); assert(arg_int(out, "papers", 0) == 2); cJSON_Delete(out);
  assert(sqlite3_exec(db, "INSERT INTO search(search) VALUES('delete-all')", NULL, NULL, NULL) == SQLITE_OK);
  out = success(tool_status(NULL)); assert(arg_int(out, "papers", -1) == 0); cJSON_Delete(out);
  assert(sqlite3_prepare_v2(db, "INSERT INTO search(rowid,title,abstract,body) VALUES(1,'Title','Abstract',?)", -1, &statement, NULL) == SQLITE_OK);
  sqlite3_bind_text(statement, 1, body, -1, SQLITE_TRANSIENT);
  assert(sqlite3_step(statement) == SQLITE_DONE); sqlite3_finalize(statement);
  sqlite3_close(db);
  if (argc != 2) unlink(path);
  puts("PASS: native SQLite BM25, category filters, Unicode pagination, metadata, status and input limits");
  return 0;
}
