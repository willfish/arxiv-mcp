#include "tools.h"
#include <ctype.h>
#include <limits.h>
#include <locale.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sqlite3.h>
#include <time.h>
#include <wchar.h>
#include <wctype.h>
#include <zlib.h>

static sqlite3 *database;
static char failure[256];
static double deadline;
static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}
static int expired(void *unused) { (void)unused; return now() > deadline; }
static void document_count(const Fts5ExtensionApi *api, Fts5Context *fts,
                           sqlite3_context *context, int argc, sqlite3_value **argv) {
  (void)argc; (void)argv;
  sqlite3_int64 rows = 0;
  int rc = api->xRowCount(fts, &rows);
  if (rc == SQLITE_OK) sqlite3_result_int64(context, rows);
  else sqlite3_result_error_code(context, rc);
}
static int register_count(void) {
  fts5_api *api = NULL;
  sqlite3_stmt *statement = NULL;
  int rc = sqlite3_prepare_v2(database, "SELECT fts5(?1)", -1, &statement, NULL);
  if (rc != SQLITE_OK) return rc;
  sqlite3_bind_pointer(statement, 1, &api, "fts5_api_ptr", NULL);
  sqlite3_step(statement);
  sqlite3_finalize(statement);
  return api ? api->xCreateFunction(api, "arxiv_document_count", NULL, document_count, NULL) : SQLITE_ERROR;
}
static int open_database(void) {
  if (database) return 1;
  const char *path = getenv("ARXIV_DATABASE");
  if (!path || !*path) path = "/srv/media/arxiv/library.sqlite3";
  if (sqlite3_open_v2(path, &database, SQLITE_OPEN_READONLY, NULL) != SQLITE_OK) {
    snprintf(failure, sizeof failure, "%s", sqlite3_errmsg(database));
    sqlite3_close(database); database = NULL; return 0;
  }
  if (register_count() != SQLITE_OK) {
    snprintf(failure, sizeof failure, "SQLite FTS5 row-count API unavailable");
    sqlite3_close(database); database = NULL; return 0;
  }
  sqlite3_busy_timeout(database, 30000);
  sqlite3_exec(database, "PRAGMA query_only=ON", NULL, NULL, NULL);
  return 1;
}
static Result json_result(cJSON *value) {
  char *text = cJSON_PrintUnformatted(value);
  cJSON_Delete(value);
  return result_ok(text);
}
static void field(cJSON *obj, const char *name, sqlite3_stmt *row, int col) {
  const char *value = (const char *)sqlite3_column_text(row, col);
  if (value) cJSON_AddStringToObject(obj, name, value);
  else cJSON_AddNullToObject(obj, name);
}
static char *paper_url(const char *id) {
  size_t size = strlen(id);
  char *url = malloc(size * 3 + 24);
  if (!url) return NULL;
  strcpy(url, "https://arxiv.org/abs/");
  char *p = url + strlen(url);
  for (const unsigned char *s = (const unsigned char *)id; *s; ++s) {
    if (isalnum(*s) || strchr("-._~/", *s)) *p++ = (char)*s;
    else { snprintf(p, 4, "%%%02X", *s); p += 3; }
  }
  *p = 0; return url;
}
static size_t character_offset(const char *text, size_t bytes, size_t chars) {
  size_t pos = 0;
  while (pos < bytes && chars) {
    ++pos;
    while (pos < bytes && ((unsigned char)text[pos] & 0xc0) == 0x80) ++pos;
    --chars;
  }
  return pos;
}
static cJSON *read_paper(const char *id, sqlite3_int64 rowid, int offset, int length) {
  const char *columns = "SELECT paper_id,title,abstract,category,license,sha256,text_chars,text_bytes,body FROM papers WHERE ";
  char sql[256];
  snprintf(sql, sizeof sql, "%s%s", columns, id ? "paper_id=?" : "id=?");
  sqlite3_stmt *row = NULL;
  if (sqlite3_prepare_v2(database, sql, -1, &row, NULL) != SQLITE_OK) return NULL;
  if (id) sqlite3_bind_text(row, 1, id, -1, SQLITE_TRANSIENT);
  else sqlite3_bind_int64(row, 1, rowid);
  if (sqlite3_step(row) != SQLITE_ROW) { sqlite3_finalize(row); return NULL; }
  sqlite3_int64 bytes = sqlite3_column_int64(row, 7);
  if (bytes < 0 || (uint64_t)bytes >= SIZE_MAX || (uint64_t)bytes >= ULONG_MAX) {
    sqlite3_finalize(row); return NULL;
  }
  char *text = malloc((size_t)bytes + 1);
  uLongf count = bytes ? (uLongf)bytes : 1;
  if (!text || uncompress((Bytef *)text, &count, sqlite3_column_blob(row, 8),
      (uLong)sqlite3_column_bytes(row, 8)) != Z_OK || count != (uLongf)bytes) {
    free(text); sqlite3_finalize(row); return NULL;
  }
  text[bytes] = 0;
  if (memchr(text, 0, (size_t)bytes)) {
    free(text); sqlite3_finalize(row); return NULL;
  }
  cJSON *out = cJSON_CreateObject();
  const char *names[] = {"paper_id", "title", "abstract", "category", "license", "sha256"};
  for (int i = 0; i < 6; ++i) field(out, names[i], row, i);
  sqlite3_int64 chars = sqlite3_column_int64(row, 6);
  cJSON_AddNumberToObject(out, "text_chars", (double)chars);
  cJSON_AddNumberToObject(out, "text_bytes", (double)bytes);
  char *url = paper_url((const char *)sqlite3_column_text(row, 0));
  cJSON_AddStringToObject(out, "url", url ? url : ""); free(url);
  cJSON_AddStringToObject(out, "format", "latex");
  size_t start = character_offset(text, (size_t)bytes, (size_t)offset);
  size_t end = start + character_offset(text + start, (size_t)bytes - start, (size_t)length);
  text[end] = 0;
  cJSON_AddStringToObject(out, "text", text + start);
  cJSON_AddNumberToObject(out, "offset", offset);
  if ((sqlite3_int64)offset + length < chars)
    cJSON_AddNumberToObject(out, "next_offset", offset + length);
  else cJSON_AddNullToObject(out, "next_offset");
  free(text); sqlite3_finalize(row); return out;
}
Result tool_paper(const cJSON *args) {
  const char *id = arg_str(args, "paper_id");
  int offset = arg_int(args, "offset", 0), length = arg_int(args, "length", 12000);
  if (!id || !*id || offset < 0 || length < 1 || length > 50000)
    return result_err("paper_id required; offset >= 0; length 1..50000");
  if (!open_database()) return result_err(failure);
  cJSON *out = read_paper(id, 0, offset, length);
  return out ? json_result(out) : result_err("Paper missing or body corrupt");
}
static char *expression(const char *query) {
  if (!query || strlen(query) > 4000) return NULL;
  if (!setlocale(LC_CTYPE, "C.UTF-8") && !setlocale(LC_CTYPE, "en_US.UTF-8")) return NULL;
  char *out = calloc(strlen(query) * 3 + 256, 1);
  if (!out) return NULL;
  size_t pos = 0, chars = 0; int words = 0, in_word = 0;
  mbstate_t state = {0};
  while (*query) {
    wchar_t c;
    size_t n = mbrtowc(&c, query, strlen(query), &state);
    if (n == (size_t)-1 || n == (size_t)-2 || !n || ++chars > 1000) { free(out); return NULL; }
    if (iswalnum(c) || c == L'_') {
      if (!in_word) {
        if (++words > 32) { free(out); return NULL; }
        if (words > 1) { memcpy(out + pos, " AND ", 5); pos += 5; }
        out[pos++] = '"'; in_word = 1;
      }
      memcpy(out + pos, query, n); pos += n;
    } else if (in_word) { out[pos++] = '"'; in_word = 0; }
    query += n;
  }
  if (in_word) out[pos++] = '"';
  out[pos] = 0;
  if (!words) { free(out); return NULL; }
  return out;
}
Result tool_search(const cJSON *args) {
  int limit = arg_int(args, "limit", 10);
  const char *mode = arg_str(args, "mode"), *category = arg_str(args, "category");
  if (mode && strcmp(mode, "bm25")) return result_err("This native server supports BM25; retained vectors are unchanged");
  if (limit < 1 || limit > 50) return result_err("limit must be 1..50");
  char *match = expression(arg_str(args, "query"));
  if (!match) return result_err("query must contain 1..32 words and at most 1000 characters");
  if (!open_database()) { free(match); return result_err(failure); }
  const char *sql = category && *category ?
    "SELECT p.id,bm25(search,5.0,2.0,1.0) AS score FROM search JOIN papers p ON p.id=search.rowid WHERE search MATCH ? AND p.category=? ORDER BY score,p.id LIMIT ?" :
    "SELECT rowid,bm25(search,5.0,2.0,1.0) AS score FROM search WHERE search MATCH ? ORDER BY score,rowid LIMIT ?";
  sqlite3_stmt *statement = NULL;
  if (sqlite3_prepare_v2(database, sql, -1, &statement, NULL) != SQLITE_OK) { free(match); return result_err(sqlite3_errmsg(database)); }
  sqlite3_bind_text(statement, 1, match, -1, SQLITE_TRANSIENT); free(match);
  int parameter = 2;
  if (category && *category) sqlite3_bind_text(statement, parameter++, category, -1, SQLITE_TRANSIENT);
  sqlite3_bind_int(statement, parameter, limit);
  deadline = now() + 30;
  sqlite3_progress_handler(database, 10000, expired, NULL);
  sqlite3_int64 ids[50]; int count = 0, rc;
  while ((rc = sqlite3_step(statement)) == SQLITE_ROW && count < limit)
    ids[count++] = sqlite3_column_int64(statement, 0);
  sqlite3_finalize(statement);
  sqlite3_progress_handler(database, 0, NULL, NULL);
  if (rc != SQLITE_DONE) return result_err(sqlite3_errmsg(database));
  cJSON *out = cJSON_CreateObject();
  cJSON_AddStringToObject(out, "mode", "bm25");
  cJSON *papers = cJSON_AddArrayToObject(out, "papers");
  /* Discovery needs identifiers and titles, never compressed bodies. */
  if (sqlite3_prepare_v2(database, "SELECT paper_id,title FROM papers WHERE id=?", -1, &statement, NULL) != SQLITE_OK) {
    cJSON_Delete(out); return result_err(sqlite3_errmsg(database));
  }
  for (int i = 0; i < count; ++i) {
    sqlite3_bind_int64(statement, 1, ids[i]);
    if (sqlite3_step(statement) != SQLITE_ROW) {
      sqlite3_finalize(statement); cJSON_Delete(out);
      return result_err("Matched paper metadata missing or unreadable");
    }
    cJSON *paper = cJSON_CreateObject();
    field(paper, "paper_id", statement, 0);
    field(paper, "title", statement, 1);
    cJSON_AddItemToArray(papers, paper);
    sqlite3_reset(statement);
  }
  sqlite3_finalize(statement);
  return json_result(out);
}
Result tool_status(const cJSON *args) {
  (void)args;
  if (!open_database()) return result_err(failure);
  cJSON *out = cJSON_CreateObject();
  sqlite3_stmt *row = NULL;
  /* FTS5 maintains this count transactionally. Do not scan millions of
     scattered index pages just to report status on rotating NAS storage. */
  if (sqlite3_prepare_v2(database, "SELECT arxiv_document_count(search) FROM search LIMIT 1", -1, &row, NULL) != SQLITE_OK) goto error;
  int first = sqlite3_step(row);
  if (first != SQLITE_ROW && first != SQLITE_DONE) goto error;
  cJSON_AddNumberToObject(out, "papers", first == SQLITE_ROW ? (double)sqlite3_column_int64(row, 0) : 0);
  cJSON_AddStringToObject(out, "count_basis", "indexed_documents");
  sqlite3_finalize(row); row = NULL;
  if (sqlite3_prepare_v2(database, "SELECT key,value FROM settings", -1, &row, NULL) != SQLITE_OK) goto error;
  cJSON *settings = cJSON_AddObjectToObject(out, "settings"); int rc;
  while ((rc = sqlite3_step(row)) == SQLITE_ROW) field(settings, (const char *)sqlite3_column_text(row, 0), row, 1);
  if (rc != SQLITE_DONE) goto error;
  sqlite3_finalize(row); row = NULL;
  if (sqlite3_prepare_v2(database, "SELECT * FROM imports ORDER BY shard", -1, &row, NULL) != SQLITE_OK) goto error;
  cJSON *imports = cJSON_AddArrayToObject(out, "imports");
  while ((rc = sqlite3_step(row)) == SQLITE_ROW) {
    cJSON *item = cJSON_CreateObject();
    for (int i = 0; i < sqlite3_column_count(row); ++i) {
      if (sqlite3_column_type(row, i) == SQLITE_INTEGER)
        cJSON_AddNumberToObject(item, sqlite3_column_name(row, i), (double)sqlite3_column_int64(row, i));
      else field(item, sqlite3_column_name(row, i), row, i);
    }
    cJSON_AddItemToArray(imports, item);
  }
  if (rc != SQLITE_DONE) goto error;
  sqlite3_finalize(row);
  cJSON_AddNullToObject(out, "embedding_index");
  return json_result(out);
error:
  sqlite3_finalize(row); cJSON_Delete(out); return result_err(sqlite3_errmsg(database));
}
