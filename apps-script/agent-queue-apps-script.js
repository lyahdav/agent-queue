var PROJECT_HEADERS = [
  "projectId",
  "enabled",
  "sheetName",
  "repoPath",
  "defaultBranch",
  "agent",
  "tdd",
  "verifyCommand",
  "pollSeconds"
];

var TASK_HEADERS = [
  "id",
  "status",
  "task",
  "commitShas",
  "redoReason",
  "claimedAt",
  "updatedAt",
  "lastRuntime",
  "lastError"
];

function doGet(e) {
  return withLock_(function () {
    var action = param_(e, "action") || "projects";
    if (action === "projects") {
      return json_({ projects: listProjects_() });
    }
    return json_({ error: "Unknown GET action: " + action });
  });
}

function doPost(e) {
  return withLock_(function () {
    var requestData = JSON.parse(e.postData.contents || "{}");
    var action = requestData.action;
    if (action === "claim") return json_(claimTask_(requestData));
    if (action === "update") return json_(updateTask_(requestData));
    if (action === "insert") return json_(insertTasks_(requestData));
    return json_({ error: "Unknown POST action: " + action });
  });
}

function withLock_(fn) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function param_(e, name) {
  return e && e.parameter && e.parameter[name] ? e.parameter[name] : "";
}

function sheet_(name) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sheet) throw new Error("Missing sheet: " + name);
  return sheet;
}

function truthy_(value) {
  var text = String(value || "").trim().toLowerCase();
  return ["1", "true", "yes", "y", "on", "enabled"].indexOf(text) !== -1;
}

function rowsAsObjects_(sheet, headers) {
  var values = sheet.getDataRange().getValues();
  var rows = [];
  for (var i = 1; i < values.length; i++) {
    var obj = {};
    for (var j = 0; j < headers.length; j++) obj[headers[j]] = values[i][j];
    obj._row = i + 1;
    rows.push(obj);
  }
  return rows;
}

function listProjects_() {
  var rows = rowsAsObjects_(sheet_("Projects"), PROJECT_HEADERS);
  return rows
    .filter(function (row) { return row.projectId; })
    .map(function (row) {
      return {
        projectId: String(row.projectId).trim(),
        enabled: truthy_(row.enabled),
        sheetName: String(row.sheetName || row.projectId).trim(),
        repoPath: String(row.repoPath || "").trim(),
        defaultBranch: String(row.defaultBranch || "main").trim(),
        agent: String(row.agent || "codex").trim(),
        tdd: truthy_(row.tdd),
        verifyCommand: String(row.verifyCommand || "").trim(),
        pollSeconds: row.pollSeconds || 30
      };
    });
}

function projectById_(projectId) {
  var projects = listProjects_();
  for (var i = 0; i < projects.length; i++) {
    if (projects[i].projectId === projectId) return projects[i];
  }
  return null;
}

function taskRows_(project) {
  return rowsAsObjects_(sheet_(project.sheetName), TASK_HEADERS);
}

function maxTaskId_(rows) {
  var maxId = 0;
  for (var i = 0; i < rows.length; i++) {
    var n = typeof rows[i].id === "number" ? rows[i].id : parseInt(rows[i].id, 10);
    if (!isNaN(n) && n > maxId) maxId = n;
  }
  return maxId;
}

function taskPayload_(row, status, claimedFrom, resume) {
  return {
    id: String(row.id),
    status: status || String(row.status || ""),
    task: String(row.task || ""),
    commitShas: String(row.commitShas || ""),
    redoReason: String(row.redoReason || ""),
    claimedFrom: claimedFrom || String(row.status || ""),
    resume: !!resume
  };
}

function claimTask_(requestData) {
  var project = projectById_(String(requestData.projectId || ""));
  if (!project) return { error: "Project not found" };
  if (!project.enabled) return { task: null, disabled: true };

  var sheet = sheet_(project.sheetName);
  var rows = taskRows_(project);
  var now = new Date();

  for (var i = 0; i < rows.length; i++) {
    var status = String(rows[i].status || "");
    if ((status === "IN PROGRESS" || status === "PLAN IN PROGRESS") && rows[i].task) {
      return { task: taskPayload_(rows[i], status, status, true) };
    }
  }

  if (requestData.resumeOnly) return { task: null };

  var maxId = maxTaskId_(rows);
  for (var j = 0; j < rows.length; j++) {
    var originalStatus = String(rows[j].status || "");
    var actionable = originalStatus === "READY" || originalStatus === "REDO" || originalStatus === "PLAN";
    if (!actionable || !rows[j].task) continue;

    if (rows[j].id === "" || rows[j].id === null || rows[j].id === undefined) {
      maxId += 1;
      rows[j].id = maxId;
      sheet.getRange(rows[j]._row, 1).setValue(maxId);
    }

    var nextStatus = originalStatus === "PLAN" ? "PLAN IN PROGRESS" : "IN PROGRESS";
    sheet.getRange(rows[j]._row, 2).setValue(nextStatus);
    sheet.getRange(rows[j]._row, 6).setValue(now);
    sheet.getRange(rows[j]._row, 7).setValue(now);
    sheet.getRange(rows[j]._row, 8).setValue("");
    sheet.getRange(rows[j]._row, 9).setValue("");
    return { task: taskPayload_(rows[j], nextStatus, originalStatus, false) };
  }

  return { task: null };
}

function updateTask_(requestData) {
  var project = projectById_(String(requestData.projectId || ""));
  if (!project) return { error: "Project not found" };
  var sheet = sheet_(project.sheetName);
  var rows = taskRows_(project);
  var id = String(requestData.id || "");
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i].id) !== id) continue;
    sheet.getRange(rows[i]._row, 2).setValue(requestData.status || "");
    if (requestData.sha) {
      var existingSha = rows[i].commitShas ? String(rows[i].commitShas).trim() : "";
      var newSha = existingSha ? existingSha + "," + requestData.sha : requestData.sha;
      sheet.getRange(rows[i]._row, 4).setValue(newSha);
    }
    if (requestData.reason) sheet.getRange(rows[i]._row, 5).setValue(requestData.reason);
    sheet.getRange(rows[i]._row, 7).setValue(new Date());
    if ("runtime" in requestData) sheet.getRange(rows[i]._row, 8).setValue(requestData.runtime || "");
    if (requestData.lastError) sheet.getRange(rows[i]._row, 9).setValue(requestData.lastError);
    return { success: true };
  }
  return { error: "Task ID not found" };
}

function insertTasks_(requestData) {
  var project = projectById_(String(requestData.projectId || ""));
  if (!project) return { error: "Project not found" };
  var sheet = sheet_(project.sheetName);
  var tasks = requestData.tasks || [];
  var inserted = 0;
  for (var i = 0; i < tasks.length; i++) {
    var task = tasks[i] || {};
    if (!task.task) continue;
    sheet.appendRow(["", task.status || "READY", task.task, "", "", "", "", "", ""]);
    inserted += 1;
  }
  return { success: true, inserted: inserted };
}
