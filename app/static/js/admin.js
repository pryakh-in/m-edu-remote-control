/* Пульт учителя: очередь заявок, назначение роботов, статусы и превью. */
(function () {
  "use strict";

  var POLL_MS = 3000;

  var grid = document.getElementById("robot-grid");
  var queueList = document.getElementById("queue-list");
  var queueEmpty = document.getElementById("queue-empty");
  var queueCount = document.getElementById("queue-count");
  var connectedList = document.getElementById("connected-list");
  var connectedEmpty = document.getElementById("connected-empty");
  var connectedCount = document.getElementById("connected-count");
  var pollChip = document.getElementById("poll-chip");
  var previewToggle = document.getElementById("preview-toggle");

  var cards = {};
  var previewsEnabled = window.localStorage.getItem("medu_previews") === "1";
  previewToggle.checked = previewsEnabled;

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (response) {
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

  function statusChip(robot) {
    if (!robot.enabled) {
      return { cls: "chip", text: "отключён" };
    }
    if (!robot.panel_online) {
      return { cls: "chip chip-danger", text: "недоступен" };
    }
    if (!robot.ros_online) {
      return { cls: "chip chip-warn", text: "робот не отвечает" };
    }
    return { cls: "chip chip-ok", text: robot.status_label.toLowerCase() };
  }

  function buildCard(robot) {
    var card = document.createElement("div");
    card.className = "robot-card";
    card.innerHTML =
      '<div class="robot-head"><h3></h3><span class="chip"></span></div>' +
      '<div class="robot-meta"></div>' +
      '<div class="preview" hidden><div class="preview-hint">Превью выключено</div></div>' +
      '<div class="robot-occupant empty"></div>' +
      '<div class="assign-row"><select></select>' +
      '<button class="btn btn-sm" data-role="assign">Подключить</button></div>' +
      '<div class="robot-actions">' +
      '<button class="btn btn-ghost btn-sm" data-role="open">Открыть панель</button>' +
      '<button class="btn btn-ghost btn-sm" data-role="free">Освободить</button>' +
      '<button class="btn btn-danger btn-sm" data-role="stop">Стоп</button>' +
      "</div>";

    card.querySelector('[data-role="assign"]').addEventListener("click", function () {
      var select = card.querySelector("select");
      if (!select.value) {
        return;
      }
      post("/admin/api/assign", { robot_id: robot.id, student_id: select.value })
        .then(refresh)
        .catch(function (error) {
          window.alert(error.message);
        });
    });

    card.querySelector('[data-role="free"]').addEventListener("click", function () {
      post("/admin/api/free", { robot_id: robot.id }).then(refresh);
    });

    card.querySelector('[data-role="open"]').addEventListener("click", function () {
      post("/admin/api/open", { robot_id: robot.id }).then(function (data) {
        if (data && data.panel_url) {
          window.open(data.panel_url, "_blank", "noopener");
        }
      });
    });

    card.querySelector('[data-role="stop"]').addEventListener("click", function () {
      post("/admin/api/stop-robot", { robot_id: robot.id })
        .then(function () {
          window.alert("Команда остановки отправлена роботу «" + robot.name + "».");
        })
        .catch(function (error) {
          window.alert(error.message);
        });
    });

    grid.appendChild(card);
    return card;
  }

  function syncSelect(select, students) {
    var signature = students
      .map(function (student) {
        return student.id;
      })
      .join(",");
    if (select.dataset.signature === signature) {
      return;
    }
    var previous = select.value;
    select.dataset.signature = signature;
    select.innerHTML = "";

    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = students.length ? "— выберите ученика —" : "очередь пуста";
    select.appendChild(placeholder);

    students.forEach(function (student) {
      var option = document.createElement("option");
      option.value = student.id;
      option.textContent = student.name + (student.online ? "" : " (не в сети)");
      select.appendChild(option);
    });

    if (
      students.some(function (student) {
        return student.id === previous;
      })
    ) {
      select.value = previous;
    }
  }

  function syncPreview(card, robot) {
    var preview = card.querySelector(".preview");
    var hint = preview.querySelector(".preview-hint");
    var frame = preview.querySelector("iframe");

    if (!previewsEnabled) {
      preview.hidden = true;
      if (frame) {
        frame.remove();
      }
      return;
    }

    preview.hidden = false;
    if (!robot.panel_online) {
      if (frame) {
        frame.remove();
      }
      hint.hidden = false;
      hint.textContent = "Панель недоступна";
      return;
    }
    if (!robot.teacher_panel_url) {
      hint.hidden = false;
      hint.textContent = "Готовим превью…";
      post("/admin/api/open", { robot_id: robot.id });
      return;
    }
    if (!frame) {
      frame = document.createElement("iframe");
      frame.setAttribute("title", "Превью панели " + robot.name);
      frame.setAttribute("scrolling", "no");
      frame.src = robot.teacher_panel_url;
      preview.appendChild(frame);
    }
    hint.hidden = true;
  }

  function renderRobots(robots, queue) {
    robots.forEach(function (robot) {
      var card = cards[robot.id];
      if (!card) {
        card = cards[robot.id] = buildCard(robot);
      }

      card.querySelector("h3").textContent = robot.name;
      var chip = statusChip(robot);
      var chipNode = card.querySelector(".robot-head .chip");
      chipNode.className = chip.cls;
      chipNode.textContent = chip.text;

      var meta = [];
      if (robot.software_version) {
        meta.push("ПО " + robot.software_version);
      }
      if (robot.host) {
        meta.push(robot.host);
      }
      if (robot.error) {
        meta.push(robot.error);
      }
      card.querySelector(".robot-meta").innerHTML = meta
        .map(function (item) {
          return '<span class="chip">' + escapeHtml(item) + "</span>";
        })
        .join("");

      var occupant = card.querySelector(".robot-occupant");
      if (robot.student) {
        occupant.className = "robot-occupant";
        occupant.innerHTML = "Управляет: <strong>" + escapeHtml(robot.student.name) + "</strong>";
      } else {
        occupant.className = "robot-occupant empty";
        occupant.textContent = "Свободен — можно подключить ученика";
      }

      card.classList.toggle("busy", Boolean(robot.student));
      card.classList.toggle("offline", !robot.panel_online);
      var select = card.querySelector("select");
      syncSelect(select, queue);
      select.disabled = queue.length === 0;
      card.querySelector('[data-role="assign"]').disabled = queue.length === 0;
      card.querySelector('[data-role="free"]').disabled = !robot.student;
      syncPreview(card, robot);
    });

    Object.keys(cards).forEach(function (id) {
      if (
        !robots.some(function (robot) {
          return robot.id === id;
        })
      ) {
        cards[id].remove();
        delete cards[id];
      }
    });
  }

  function renderQueue(queue, robots) {
    queueCount.textContent = queue.length;
    queueEmpty.hidden = queue.length > 0;
    queueList.innerHTML = "";

    var freeRobots = robots.filter(function (robot) {
      return robot.enabled && !robot.student;
    });

    queue.forEach(function (student) {
      var item = document.createElement("li");
      item.className = "queue-item" + (student.online ? "" : " offline");

      var who = document.createElement("div");
      who.className = "who";
      who.innerHTML =
        "<strong>" +
        escapeHtml(student.name) +
        "</strong><span class='muted'>ждёт " +
        formatDuration(student.waiting_for) +
        (student.online ? "" : " · не в сети") +
        "</span>";

      var actions = document.createElement("div");
      actions.className = "queue-actions";

      var select = document.createElement("select");
      var placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = freeRobots.length ? "робот…" : "нет свободных";
      select.appendChild(placeholder);
      freeRobots.forEach(function (robot) {
        var option = document.createElement("option");
        option.value = robot.id;
        option.textContent = robot.name;
        select.appendChild(option);
      });

      select.disabled = freeRobots.length === 0;

      var connect = document.createElement("button");
      connect.className = "btn btn-sm";
      connect.textContent = "Подключить";
      connect.disabled = freeRobots.length === 0;
      connect.addEventListener("click", function () {
        if (!select.value) {
          return;
        }
        post("/admin/api/assign", { robot_id: select.value, student_id: student.id })
          .then(refresh)
          .catch(function (error) {
            window.alert(error.message);
          });
      });

      var remove = document.createElement("button");
      remove.className = "btn btn-ghost btn-sm";
      remove.textContent = "Убрать";
      remove.title = "Удалить заявку из очереди";
      remove.addEventListener("click", function () {
        post("/admin/api/remove", { student_id: student.id }).then(refresh);
      });

      actions.appendChild(select);
      actions.appendChild(connect);
      actions.appendChild(remove);
      item.appendChild(who);
      item.appendChild(actions);
      queueList.appendChild(item);
    });
  }

  function renderConnected(connected, robots) {
    connectedCount.textContent = connected.length;
    connectedEmpty.hidden = connected.length > 0;
    connectedList.innerHTML = "";

    connected.forEach(function (student) {
      var robot = robots.filter(function (item) {
        return item.id === student.robot_id;
      })[0];

      var item = document.createElement("li");
      item.className = "queue-item" + (student.online ? "" : " offline");
      item.innerHTML =
        "<div class='who'><strong>" +
        escapeHtml(student.name) +
        "</strong><span class='muted'>" +
        escapeHtml(robot ? robot.name : "робот удалён") +
        (student.online ? "" : " · вкладка закрыта") +
        "</span></div>";

      var actions = document.createElement("div");
      actions.className = "queue-actions";

      var back = document.createElement("button");
      back.className = "btn btn-ghost btn-sm";
      back.textContent = "В очередь";
      back.addEventListener("click", function () {
        post("/admin/api/disconnect", { student_id: student.id }).then(refresh);
      });

      var remove = document.createElement("button");
      remove.className = "btn btn-danger btn-sm";
      remove.textContent = "Отключить";
      remove.addEventListener("click", function () {
        post("/admin/api/remove", { student_id: student.id }).then(refresh);
      });

      actions.appendChild(back);
      actions.appendChild(remove);
      item.appendChild(actions);
      connectedList.appendChild(item);
    });
  }

  function refresh() {
    return fetch("/admin/api/state", { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/admin/login";
          return null;
        }
        return response.json();
      })
      .then(function (state) {
        if (!state) {
          return;
        }
        pollChip.className = "chip chip-ok";
        pollChip.innerHTML = "<span class='dot'></span> данные обновлены";
        renderRobots(state.robots, state.queue);
        renderQueue(state.queue, state.robots);
        renderConnected(state.connected, state.robots);
      })
      .catch(function () {
        pollChip.className = "chip chip-danger";
        pollChip.innerHTML = "<span class='dot'></span> нет связи с сервером";
      });
  }

  function formatDuration(seconds) {
    if (seconds < 60) {
      return seconds + " с";
    }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return minutes + " мин";
    }
    return Math.floor(minutes / 60) + " ч " + (minutes % 60) + " мин";
  }

  function escapeHtml(value) {
    var node = document.createElement("span");
    node.textContent = String(value == null ? "" : value);
    return node.innerHTML;
  }

  previewToggle.addEventListener("change", function () {
    previewsEnabled = previewToggle.checked;
    window.localStorage.setItem("medu_previews", previewsEnabled ? "1" : "0");
    refresh();
  });

  document.addEventListener("click", function (event) {
    var target = event.target.closest('[data-action="refresh"]');
    if (!target) {
      return;
    }
    target.disabled = true;
    post("/admin/api/refresh")
      .then(refresh)
      .finally(function () {
        target.disabled = false;
      });
  });

  refresh();
  window.setInterval(refresh, POLL_MS);
})();
