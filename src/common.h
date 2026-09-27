#pragma once

#define _POSIX_C_SOURCE 200809L
#if defined(__APPLE__) && !defined(_DARWIN_C_SOURCE)
/* Darwin keeps mkdtemp and related declarations behind its extension flag. */
#define _DARWIN_C_SOURCE
#endif

#include <cjson/cJSON.h>
#include <stddef.h>

typedef struct {
  char *text;
  int is_error;
} Result;

Result result_ok(char *text);
Result result_err(const char *msg);
void result_free(Result r);

typedef struct {
  char **v;
  size_t n;
  size_t cap;
} Argv;

void argv_init(Argv *a);
void argv_add(Argv *a, const char *s);
void argv_free(Argv *a);

typedef struct {
  char *out;
  char *err;
  int status;
} Capture;

void capture_free(Capture *c);
int run_cmd_input(char *const argv[], Capture *cap, const char *input);

const char *arg_str(const cJSON *args, const char *key);
int arg_int(const cJSON *args, const char *key, int fallback);
