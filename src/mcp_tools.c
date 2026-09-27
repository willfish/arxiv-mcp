/* Himalaya-style adapter: fixed argv, JSON on stdin, no shell. */
#include "tools.h"
#include <stdlib.h>
#include <string.h>

static Result invoke(const char *operation, const cJSON *args) {
  const char *binary = getenv("ARXIV_BINARY");
  Argv command;
  argv_init(&command);
  argv_add(&command, binary && *binary ? binary : "arxiv");
  argv_add(&command, operation);
  argv_add(&command, "--json-input");
  argv_add(&command, NULL);
  char *input = args ? cJSON_PrintUnformatted(args) : strdup("{}");
  Capture output = {0};
  int rc = run_cmd_input(command.v, &output, input);
  free(input);
  argv_free(&command);
  Result result;
  if (rc < 0) result = result_err("Backend timed out or exceeded capture limits");
  else if (output.status) result = result_err(output.err && *output.err ? output.err : "Backend failed");
  else {
    cJSON *payload = cJSON_ParseWithOpts(output.out, NULL, 1);
    result = cJSON_IsObject(payload) ? result_ok(strdup(output.out)) : result_err("Backend returned invalid JSON");
    cJSON_Delete(payload);
  }
  capture_free(&output);
  return result;
}
Result tool_search(const cJSON *args) { return invoke("search", args); }
Result tool_paper(const cJSON *args) { return invoke("show", args); }
Result tool_status(const cJSON *args) { return invoke("status", args); }
