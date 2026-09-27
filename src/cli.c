#include "tools.h"
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int usage(FILE *out) {
  fputs("Usage: arxiv search QUERY [--limit N] [--category CATEGORY]\n"
        "       arxiv show PAPER_ID [--offset N] [--length N]\n"
        "       arxiv status\n"
        "All commands accept --database PATH and return JSON.\n"
        "MCP adapter mode: COMMAND --json-input (JSON object on stdin).\n", out);
  return out == stdout ? 0 : 2;
}
int main(int argc, char **argv) {
  if (argc == 2 && !strcmp(argv[1], "--help")) return usage(stdout);
  if (argc < 2) return usage(stderr);
  const char *operation = argv[1];
  int search = !strcmp(operation, "search"), show = !strcmp(operation, "show");
  if (!search && !show && strcmp(operation, "status")) return usage(stderr);
  cJSON *args = cJSON_CreateObject();
  int json_input = 0, supplied = 0;
  for (int i = 2; i < argc; ++i) {
    const char *key = argv[i];
    if (!strcmp(key, "--json-input") && !supplied) { json_input = 1; continue; }
    if (json_input && strcmp(key, "--database")) goto invalid;
    if (!strcmp(key, "--database")) {
      if (++i >= argc) goto invalid;
      if (setenv("ARXIV_DATABASE", argv[i], 1)) goto invalid;
    } else if (search && !strcmp(key, "--category")) {
      if (++i >= argc) goto invalid;
      cJSON_AddStringToObject(args, "category", argv[i]); supplied = 1;
    } else if ((search && !strcmp(key, "--limit")) ||
               (show && (!strcmp(key, "--offset") || !strcmp(key, "--length")))) {
      if (++i >= argc) goto invalid;
      char *end; errno = 0;
      long number = strtol(argv[i], &end, 10);
      if (errno || end == argv[i] || *end || number < 0 || number > INT_MAX) goto invalid;
      cJSON_AddNumberToObject(args, key + 2, (double)number); supplied = 1;
    } else if (*key != '-' && (search || show)) {
      const char *name = search ? "query" : "paper_id";
      if (cJSON_HasObjectItem(args, name)) goto invalid;
      cJSON_AddStringToObject(args, name, key); supplied = 1;
    } else goto invalid;
  }
  if (json_input) {
    size_t max = 1024 * 1024;
    char *raw = malloc(max + 2);
    if (!raw) goto invalid;
    size_t count = fread(raw, 1, max + 1, stdin);
    raw[count] = 0;
    cJSON_Delete(args);
    args = count <= max && !ferror(stdin) ? cJSON_ParseWithOpts(raw, NULL, 1) : NULL;
    free(raw);
    if (!cJSON_IsObject(args)) goto invalid;
  }
  Result result = search ? tool_search(args) : show ? tool_paper(args) : tool_status(args);
  cJSON_Delete(args);
  fprintf(result.is_error ? stderr : stdout, "%s\n", result.text ? result.text : "");
  int rc = result.is_error ? 1 : 0;
  result_free(result);
  return rc;
invalid:
  cJSON_Delete(args);
  return usage(stderr);
}
