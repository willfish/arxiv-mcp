#include "common.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static FILE *input, *output;
static cJSON *request(const char *json) {
  fprintf(input, "%s\n", json); fflush(input);
  char *line = NULL; size_t size = 0;
  assert(getline(&line, &size, output) > 0);
  cJSON *reply = cJSON_Parse(line); free(line); assert(reply); return reply;
}
static cJSON *tool_payload(cJSON *reply) {
  cJSON *result = cJSON_GetObjectItem(reply, "result");
  assert(result && !cJSON_IsTrue(cJSON_GetObjectItem(result, "isError")));
  cJSON *block = cJSON_GetArrayItem(cJSON_GetObjectItem(result, "content"), 0);
  cJSON *payload = cJSON_Parse(arg_str(block, "text")); assert(payload); return payload;
}
int main(void) {
  char directory[] = "/tmp/arxiv-protocol-XXXXXX"; assert(mkdtemp(directory));
  char database[PATH_MAX], binary[PATH_MAX], server[PATH_MAX], cwd[PATH_MAX];
  assert(getcwd(cwd, sizeof cwd));
  assert(snprintf(database, sizeof database, "%s/library.sqlite3", directory) < (int)sizeof database);
  assert(snprintf(binary, sizeof binary, "%s/arxiv", cwd) < (int)sizeof binary);
  assert(snprintf(server, sizeof server, "%s/arxiv-mcp", cwd) < (int)sizeof server);
  char *fixture[] = {"./backend_test", database, NULL}; Capture capture = {0};
  assert(run_cmd_input(fixture, &capture, NULL) == 0 && capture.status == 0); capture_free(&capture);
  setenv("ARXIV_DATABASE", database, 1); setenv("ARXIV_BINARY", binary, 1);
  /* Both executables must work without Python, a shell, or any PATH tools. */
  setenv("PATH", "/nonexistent", 1);
  char *show[] = {binary, "show", "old/001", "--offset", "1", "--length", "2", NULL};
  assert(run_cmd_input(show, &capture, NULL) == 0 && capture.status == 0);
  cJSON *value = cJSON_Parse(capture.out); assert(value);
  assert(!strcmp(arg_str(value, "text"), "β😀")); cJSON_Delete(value); capture_free(&capture);
  int to_server[2], from_server[2]; assert(pipe(to_server) == 0 && pipe(from_server) == 0);
  pid_t child = fork(); assert(child >= 0);
  if (!child) {
    dup2(to_server[0], STDIN_FILENO); dup2(from_server[1], STDOUT_FILENO);
    close(to_server[0]); close(to_server[1]); close(from_server[0]); close(from_server[1]);
    execl(server, server, (char *)NULL); _exit(127);
  }
  close(to_server[0]); close(from_server[1]);
  input = fdopen(to_server[1], "w"); output = fdopen(from_server[0], "r"); assert(input && output);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\"}}");
  cJSON *result = cJSON_GetObjectItem(value, "result");
  assert(!strcmp(arg_str(cJSON_GetObjectItem(result, "serverInfo"), "name"), "arxiv-mcp")); cJSON_Delete(value);
  fprintf(input, "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n"); fflush(input);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}");
  assert(cJSON_GetArraySize(cJSON_GetObjectItem(cJSON_GetObjectItem(value, "result"), "tools")) == 3); cJSON_Delete(value);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"search\",\"arguments\":{\"query\":\"rarephysicalterm\"}}}");
  cJSON *payload = tool_payload(value);
  assert(cJSON_GetArraySize(cJSON_GetObjectItem(payload, "papers")) == 1);
  cJSON *hit = cJSON_GetArrayItem(cJSON_GetObjectItem(payload, "papers"), 0);
  assert(cJSON_GetArraySize(hit) == 2);
  assert(!strcmp(arg_str(hit, "paper_id"), "old/001"));
  assert(!strcmp(arg_str(hit, "title"), "Title"));
  cJSON_Delete(payload); cJSON_Delete(value);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"paper\",\"arguments\":{\"paper_id\":\"old/001\",\"offset\":1,\"length\":2}}}");
  payload = tool_payload(value); assert(!strcmp(arg_str(payload, "text"), "β😀")); cJSON_Delete(payload); cJSON_Delete(value);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",\"params\":{\"name\":\"library_status\"}}");
  payload = tool_payload(value); assert(arg_int(payload, "papers", 0) == 1); cJSON_Delete(payload); cJSON_Delete(value);
  value = request("{\"jsonrpc\":\"2.0\",\"id\":6,\"method\":\"tools/call\",\"params\":{\"name\":\"search\",\"arguments\":{\"query\":true}}}");
  assert(cJSON_GetObjectItem(value, "error")); cJSON_Delete(value);
  value = request("not-json"); assert(cJSON_GetObjectItem(value, "error")); cJSON_Delete(value);
  fclose(input); fclose(output); int status; assert(waitpid(child, &status, 0) == child);
  assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
  unlink(database); rmdir(directory);
  puts("PASS: native CLI and real MCP subprocess lifecycle without Python/PATH dependencies");
  return 0;
}
