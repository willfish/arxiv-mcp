/* Adapted from willfish/himalaya-mcp @ 0979a1c8cf9ad4c03cbdf84f2a8895580756e5fd. */
#include "tools.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef Result (*ToolFn)(const cJSON *args);

typedef struct {
  const char *name;
  const char *description;
  const char *schema;
  ToolFn fn;
} Tool;

#include "domain.h"

static void reply(cJSON *id, cJSON *result, cJSON *error) {
  cJSON *msg = cJSON_CreateObject();
  cJSON_AddStringToObject(msg, "jsonrpc", "2.0");
  if (id) cJSON_AddItemToObject(msg, "id", cJSON_Duplicate(id, 1));
  else cJSON_AddNullToObject(msg, "id");
  if (error) cJSON_AddItemToObject(msg, "error", error);
  else cJSON_AddItemToObject(msg, "result", result);
  char *printed = cJSON_PrintUnformatted(msg);
  fputs(printed, stdout);
  fputc('\n', stdout);
  fflush(stdout);
  free(printed);
  cJSON_Delete(msg);
}

static void rpc_error(cJSON *id, int code, const char *message) {
  cJSON *error = cJSON_CreateObject();
  cJSON_AddNumberToObject(error, "code", code);
  cJSON_AddStringToObject(error, "message", message);
  reply(id, NULL, error);
}

static int valid_arguments(const Tool *tool, const cJSON *args) {
  if (args && !cJSON_IsObject(args)) return 0;
  cJSON *schema = cJSON_Parse(tool->schema);
  const cJSON *required = cJSON_GetObjectItemCaseSensitive(schema, "required"), *key;
  int valid = 1;
  cJSON_ArrayForEach(key, required) if (!cJSON_GetObjectItemCaseSensitive(args, key->valuestring)) valid = 0;
  const cJSON *props = cJSON_GetObjectItemCaseSensitive(schema, "properties"), *value;
  cJSON_ArrayForEach(value, args) {
    const cJSON *spec = cJSON_GetObjectItemCaseSensitive(props, value->string);
    const char *type = arg_str(spec, "type");
    if (!type) { valid = 0; continue; }
    if (!strcmp(type, "string") && !cJSON_IsString(value)) valid = 0;
    if (!strcmp(type, "integer") && (!cJSON_IsNumber(value) || value->valuedouble != value->valueint)) valid = 0;
    if (!strcmp(type, "boolean") && !cJSON_IsBool(value)) valid = 0;
    if (!strcmp(type, "array")) {
      if (!cJSON_IsArray(value)) valid = 0;
      const cJSON *item;
      cJSON_ArrayForEach(item, value) if (!cJSON_IsString(item)) valid = 0;
    }
  }
  cJSON_Delete(schema);
  return valid;
}

static void tool_result(cJSON *id, Result r) {
  cJSON *result = cJSON_CreateObject();
  cJSON *content = cJSON_AddArrayToObject(result, "content");
  cJSON *block = cJSON_CreateObject();
  cJSON_AddStringToObject(block, "type", "text");
  cJSON_AddStringToObject(block, "text", r.text ? r.text : "");
  cJSON_AddItemToArray(content, block);
  if (r.is_error) cJSON_AddBoolToObject(result, "isError", 1);
  reply(id, result, NULL);
  result_free(r);
}

static void handle(cJSON *msg) {
  cJSON *id = cJSON_GetObjectItemCaseSensitive(msg, "id");
  cJSON *method_item = cJSON_GetObjectItemCaseSensitive(msg, "method");
  const char *version = arg_str(msg, "jsonrpc");
  if (!cJSON_IsObject(msg) || !version || strcmp(version, "2.0") || !cJSON_IsString(method_item) || (id && !cJSON_IsString(id) && !cJSON_IsNumber(id) && !cJSON_IsNull(id))) {
    rpc_error(NULL, -32600, "invalid JSON-RPC request"); return;
  }
  if (!id) return; /* Notifications must not trigger tools or receive replies. */
  const char *method = method_item->valuestring;
  cJSON *params = cJSON_GetObjectItemCaseSensitive(msg, "params");
  if (params && !cJSON_IsObject(params)) { rpc_error(id, -32602, "params must be an object"); return; }

  if (!strcmp(method, "initialize")) {
    cJSON *result = cJSON_CreateObject();
    const cJSON *pv = params ? cJSON_GetObjectItemCaseSensitive(params, "protocolVersion") : NULL;
    cJSON_AddStringToObject(result, "protocolVersion", cJSON_IsString(pv) ? pv->valuestring : "2024-11-05");
    cJSON *caps = cJSON_AddObjectToObject(result, "capabilities");
    cJSON_AddObjectToObject(caps, "tools");
    cJSON_AddObjectToObject(caps, "prompts");
    cJSON *info = cJSON_AddObjectToObject(result, "serverInfo");
    cJSON_AddStringToObject(info, "name", "arxiv-mcp");
    cJSON_AddStringToObject(info, "version", "0.1.0");
    reply(id, result, NULL);
    return;
  }
  if (!strcmp(method, "ping")) {
    reply(id, cJSON_CreateObject(), NULL);
    return;
  }
  if (!strcmp(method, "tools/list")) {
    cJSON *result = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(result, "tools");
    for (int i = 0; i < tool_count; i++) {
      cJSON *tool = cJSON_CreateObject();
      cJSON_AddStringToObject(tool, "name", tools[i].name);
      cJSON_AddStringToObject(tool, "description", tools[i].description);
      cJSON *schema = cJSON_Parse(tools[i].schema);
      cJSON_AddItemToObject(tool, "inputSchema", schema);
      cJSON_AddItemToArray(arr, tool);
    }
    reply(id, result, NULL);
    return;
  }
  if (!strcmp(method, "tools/call")) {
    const cJSON *name = params ? cJSON_GetObjectItemCaseSensitive(params, "name") : NULL;
    const cJSON *args = params ? cJSON_GetObjectItemCaseSensitive(params, "arguments") : NULL;
    if (!cJSON_IsString(name)) {
      rpc_error(id, -32602, "missing tool name");
      return;
    }
    for (int i = 0; i < tool_count; i++) {
      if (!strcmp(tools[i].name, name->valuestring)) {
        if (!valid_arguments(&tools[i], args)) { rpc_error(id, -32602, "arguments do not match tool schema"); return; }
        tool_result(id, tools[i].fn(args));
        return;
      }
    }
    rpc_error(id, -32602, "unknown tool");
    return;
  }
  if (!strcmp(method, "prompts/list")) {
    cJSON *result = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(result, "prompts");
    for (size_t i = 0; i < sizeof prompts / sizeof prompts[0]; i++) {
      cJSON *prompt = cJSON_CreateObject();
      cJSON_AddStringToObject(prompt, "name", prompts[i].name);
      cJSON_AddStringToObject(prompt, "description", prompts[i].description);
      cJSON *arguments = cJSON_AddArrayToObject(prompt, "arguments");
      const char *names[] = {"paper_id", "query", "category", "instructions"};
      for (int j = 0; j < 4; j++) {
        cJSON *argument = cJSON_CreateObject();
        cJSON_AddStringToObject(argument, "name", names[j]);
        cJSON_AddBoolToObject(argument, "required", 0);
        cJSON_AddItemToArray(arguments, argument);
      }
      cJSON_AddItemToArray(arr, prompt);
    }
    reply(id, result, NULL);
    return;
  }
  if (!strcmp(method, "prompts/get")) {
    const cJSON *name = params ? cJSON_GetObjectItemCaseSensitive(params, "name") : NULL;
    if (!cJSON_IsString(name)) {
      rpc_error(id, -32602, "prompt name is required");
      return;
    }
    const cJSON *arguments = cJSON_GetObjectItemCaseSensitive(params, "arguments"), *argument;
    if (arguments && !cJSON_IsObject(arguments)) { rpc_error(id, -32602, "prompt arguments must be an object"); return; }
    cJSON_ArrayForEach(argument, arguments) {
      if (!cJSON_IsString(argument) || (strcmp(argument->string, "paper_id") && strcmp(argument->string, "query") && strcmp(argument->string, "category") && strcmp(argument->string, "instructions"))) {
        rpc_error(id, -32602, "unknown or non-string prompt argument"); return;
      }
    }
    for (size_t i = 0; i < sizeof prompts / sizeof prompts[0]; i++) {
      if (strcmp(prompts[i].name, name->valuestring)) continue;
      cJSON *result = cJSON_CreateObject();
      cJSON_AddStringToObject(result, "description", prompts[i].description);
      cJSON *messages = cJSON_AddArrayToObject(result, "messages");
      cJSON *message = cJSON_CreateObject();
      cJSON_AddStringToObject(message, "role", "user");
      cJSON *content = cJSON_AddObjectToObject(message, "content");
      cJSON_AddStringToObject(content, "type", "text");
      char *context = arguments ? cJSON_PrintUnformatted(arguments) : strdup("{}");
      char *text = malloc(strlen(prompts[i].text) + strlen(context) + 64);
      sprintf(text, "%s\n\nUser-supplied context:\n%s", prompts[i].text, context);
      cJSON_AddStringToObject(content, "text", text);
      free(text); free(context);
      cJSON_AddItemToArray(messages, message);
      reply(id, result, NULL);
      return;
    }
    rpc_error(id, -32602, "unknown prompt");
    return;
  }
  if (id) {
    cJSON *error = cJSON_CreateObject();
    cJSON_AddNumberToObject(error, "code", -32601);
    cJSON_AddStringToObject(error, "message", "method not found");
    reply(id, NULL, error);
  }
}

static void serve(void) {
  char *line = NULL;
  size_t cap = 0;
  while (getline(&line, &cap, stdin) != -1) {
    const char *end = NULL;
    cJSON *msg = cJSON_ParseWithOpts(line, &end, 1);
    if (msg) {
      handle(msg);
      cJSON_Delete(msg);
    } else rpc_error(NULL, -32700, "invalid JSON");
  }
  free(line);
}

int main(int argc, char **argv) {
  if (argc > 1 && !strcmp(argv[1], "doctor")) {
    Result health = tool_status(NULL);
    fputs(health.text ? health.text : "", stdout);
    int rc = health.is_error;
    result_free(health);
    return rc;
  }
  serve();
  return 0;
}
