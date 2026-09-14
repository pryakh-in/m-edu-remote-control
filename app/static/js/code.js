/* Одновременный запуск Python или JSON-программы на нескольких роботах. */
(function () {
  "use strict";

  var SAMPLES = {
    python:
      "# Безопасный пример: робот не двигается, только сообщает о себе\n" +
      'print("Привет из лаборатории 1409!")\n' +
      'print("Положение осей:", manipulator.get_joint_state())\n' +
      "\n" +
      "# Пример движения — углы в радианах:\n" +
      "# manipulator.move_to_angles(0.3, -0.5, -0.9, velocity_factor=0.2)\n" +
      "# await asyncio.sleep(1)\n" +
      "# manipulator.move_to_angles(0, -0.5, -0.9, velocity_factor=0.2)\n",
    program:
      JSON.stringify(
        {
          jointPoints: [
            [0, { id: 0, name: "Исходное положение", time: 0, positions: "0, -22.9, -45.83" }],
          ],
          cpiPoints: [],
          program: {
            blocks: {
              languageVersion: 0,
              blocks: [
                {
                  type: "controls_repeat_ext",
                  inputs: {
                    TIMES: { shadow: { type: "math_number", fields: { NUM: 2 } } },
                    DO: {
                      block: {
                        type: "move_to_point",
                        extraState: { selectedPointId: "j0" },
                        fields: {
                          velocity: 0.3,
                          acceleration: 0.3,
                          plannerType: "PlannerType.PTP",
                        },
                      },
                    },
                  },
                },
              ],
            },
          },
        },
        null,
        2
      ) + "\n",
  };

  var source = document.getElementById("source");
  var sourceLabel = document.getElementById("source-label");
  var sourceFile = document.getElementById("source-file");
  var sourceFileName = document.getElementById("source-file-name");
  var picks = document.getElementById("robot-picks");
  var results = document.getElementById("results");
  var runButton = document.getElementById("run");
  var stopButton = document.getElementById("stop");
  var sampleButton = document.getElementById("sample");
  var pickAll = document.getElementById("pick-all");
  var errorBox = document.getElementById("run-error");
  var hints = {
    python: document.getElementById("hint-python"),
    program: document.getElementById("hint-program"),
  };

  var kind = "python";
  var robots = [];
  var activeJob = null;
  var jobTimer = null;

  function request(url, options) {
    return fetch(url, options).then(function (response) {
      if (response.status === 401) {
        window.location.href = "/admin/login";
        return null;
      }
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error(data.detail || "Ошибка запроса");
        }
        return data;
      });
    });
  }

  function loadRobots() {
    return request("/admin/api/state", { headers: { Accept: "application/json" } }).then(
      function (state) {
        if (!state) {
          return;
        }
        var selected = selectedIds();
        robots = state.robots;
        picks.innerHTML = "";
        robots.forEach(function (robot) {
          var available = robot.enabled && robot.ros_online;
          var label = document.createElement("label");
          label.className = "robot-pick" + (available ? "" : " disabled");

          var checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.value = robot.id;
          checkbox.disabled = !available;
          checkbox.checked = selected.indexOf(robot.id) !== -1 && available;

          var text = document.createElement("span");
          text.innerHTML =
            "<strong>" +
            escapeHtml(robot.name) +
            "</strong><br><span class='muted'>" +
            escapeHtml(robot.status_label) +
            (robot.student ? " · управляет " + escapeHtml(robot.student.name) : "") +
            "</span>";

          label.appendChild(checkbox);
          label.appendChild(text);
          picks.appendChild(label);
        });
      }
    );
  }

  function selectedIds() {
    return Array.prototype.slice
      .call(picks.querySelectorAll("input[type=checkbox]"))
      .filter(function (input) {
        return input.checked;
      })
      .map(function (input) {
        return input.value;
      });
  }

  function setKind(next) {
    kind = next;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.classList.toggle("active", tab.getAttribute("data-kind") === next);
    });
    sourceLabel.textContent = next === "python" ? "Код на Python" : "Программа в формате JSON";
    hints.python.hidden = next !== "python";
    hints.program.hidden = next !== "program";
  }

  // SDK робота печатает много служебных строк — по умолчанию их скрываем.
  var NOISE = /^\[(PROMISE|MANIPULATOR|GET_MANAGE_CMD|INFO_CMD|CONNECT)\]/;

  function cleanOutput(text) {
    if (!document.getElementById("clean-output").checked) {
      return text;
    }
    var lines = text.split("\n").filter(function (line) {
      return !NOISE.test(line.trim());
    });
    return lines.join("\n").trim();
  }

  function statusChip(status) {
    if (status === "running") {
      return "<span class='chip chip-accent'>выполняется</span>";
    }
    if (status === "done") {
      return "<span class='chip chip-ok'>готово</span>";
    }
    if (status === "stopped") {
      return "<span class='chip chip-warn'>остановлено</span>";
    }
    return "<span class='chip chip-danger'>ошибка</span>";
  }

  function renderJob(job) {
    results.innerHTML = "";
    job.runs.forEach(function (run) {
      var block = document.createElement("div");
      block.className = "result";
      var parts =
        "<div class='result-head'><strong>" +
        escapeHtml(run.robot_name) +
        "</strong>" +
        statusChip(run.status) +
        "</div><div class='muted'>" +
        run.duration +
        " с" +
        (run.result_code !== null && run.result_code !== undefined
          ? " · код " + run.result_code
          : "") +
        "</div>";

      if (run.feedback && run.feedback.length) {
        parts += "<pre>" + escapeHtml(run.feedback.join("\n")) + "</pre>";
      }
      var output = cleanOutput(run.output);
      if (output) {
        parts += "<pre>" + escapeHtml(output) + "</pre>";
      }
      if (run.error) {
        parts += "<pre class='err'>" + escapeHtml(run.error) + "</pre>";
      }
      block.innerHTML = parts;
      results.appendChild(block);
    });

    stopButton.disabled = job.finished;
    runButton.disabled = !job.finished;
    if (job.finished && jobTimer) {
      window.clearInterval(jobTimer);
      jobTimer = null;
    }
  }

  function pollJob() {
    if (!activeJob) {
      return;
    }
    request("/admin/api/run/" + activeJob, { headers: { Accept: "application/json" } })
      .then(function (job) {
        if (job) {
          renderJob(job);
        }
      })
      .catch(function () {
        /* следующая попытка через интервал */
      });
  }

  runButton.addEventListener("click", function () {
    var robotIds = selectedIds();
    errorBox.hidden = true;

    if (!robotIds.length) {
      errorBox.textContent = "Выберите хотя бы одного робота.";
      errorBox.hidden = false;
      return;
    }
    if (!source.value.trim()) {
      errorBox.textContent = "Введите код или программу.";
      errorBox.hidden = false;
      return;
    }

    runButton.disabled = true;
    request("/admin/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        kind: kind,
        source: source.value,
        robot_ids: robotIds,
        wrap_sdk: document.getElementById("wrap-sdk").checked,
      }),
    })
      .then(function (data) {
        if (!data) {
          return;
        }
        activeJob = data.job.id;
        renderJob(data.job);
        if (jobTimer) {
          window.clearInterval(jobTimer);
        }
        jobTimer = window.setInterval(pollJob, 1200);
      })
      .catch(function (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
        runButton.disabled = false;
      });
  });

  stopButton.addEventListener("click", function () {
    if (!activeJob) {
      return;
    }
    request("/admin/api/run/" + activeJob + "/stop", { method: "POST" }).catch(function (error) {
      window.alert(error.message);
    });
  });

  sampleButton.addEventListener("click", function () {
    source.value = SAMPLES[kind];
    sourceFileName.textContent = "JSON Blockly с панели или .py";
  });

  sourceFile.addEventListener("change", function () {
    var file = sourceFile.files && sourceFile.files[0];
    if (!file) {
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result || "");
      var name = file.name || "";
      var isPython = /\.py$/i.test(name) || (!/^\s*[{\[]/.test(text) && !/\.json$/i.test(name));
      if (isPython) {
        setKind("python");
        source.value = text;
      } else {
        setKind("program");
        try {
          source.value = JSON.stringify(JSON.parse(text), null, 2) + "\n";
        } catch (error) {
          source.value = text;
        }
      }
      sourceFileName.textContent = name;
      errorBox.hidden = true;
    };
    reader.onerror = function () {
      errorBox.textContent = "Не удалось прочитать файл.";
      errorBox.hidden = false;
    };
    reader.readAsText(file, "utf-8");
    sourceFile.value = "";
  });

  document.getElementById("clean-output").addEventListener("change", pollJob);

  pickAll.addEventListener("click", function () {
    Array.prototype.forEach.call(picks.querySelectorAll("input[type=checkbox]"), function (input) {
      if (!input.disabled) {
        input.checked = true;
      }
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
    tab.addEventListener("click", function () {
      setKind(tab.getAttribute("data-kind"));
    });
  });

  function escapeHtml(value) {
    var node = document.createElement("span");
    node.textContent = String(value == null ? "" : value);
    return node.innerHTML;
  }

  setKind("python");
  loadRobots();
  window.setInterval(loadRobots, 8000);
})();
