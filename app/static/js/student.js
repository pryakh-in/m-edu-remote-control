/* Страница ученика: опрос состояния заявки и показ панели робота. */
(function () {
  "use strict";

  var POLL_MS = 2500;

  var waitingView = document.getElementById("waiting-view");
  var panelView = document.getElementById("panel-view");
  var frame = document.getElementById("panel-frame");
  var robotLabel = document.getElementById("panel-robot");
  var waitText = document.getElementById("wait-text");
  var queueBadge = document.getElementById("queue-badge");
  var queuePosition = document.getElementById("queue-position");
  var queueHint = document.getElementById("queue-hint");

  var currentPanel = null;

  function showPanel(state) {
    if (currentPanel !== state.panel_url) {
      currentPanel = state.panel_url;
      frame.src = state.panel_url;
    }
    robotLabel.textContent = state.robot_name || "Робот";
    document.body.classList.add("panel-mode");
    panelView.classList.add("active");
    waitingView.hidden = true;
  }

  function showWaiting(state) {
    if (currentPanel !== null) {
      currentPanel = null;
      frame.src = "about:blank";
    }
    document.body.classList.remove("panel-mode");
    panelView.classList.remove("active");
    waitingView.hidden = false;

    if (state.position) {
      queueBadge.hidden = false;
      queuePosition.textContent = state.position;
      waitText.textContent = "Заявка у учителя. Скоро вас подключат к роботу.";
    } else {
      queueBadge.hidden = true;
      waitText.textContent = "Ждём, пока учитель подключит вас к роботу.";
    }

    var parts = [];
    if (typeof state.queue_total === "number") {
      parts.push("всего в очереди: " + state.queue_total);
    }
    if (typeof state.robots_free === "number") {
      parts.push("свободных роботов: " + state.robots_free);
    }
    queueHint.textContent = parts.join(" · ");
  }

  function poll() {
    fetch("/api/me", { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 404) {
          window.location.href = "/";
          return null;
        }
        return response.json();
      })
      .then(function (state) {
        if (!state) {
          return;
        }
        if (state.state === "connected") {
          showPanel(state);
        } else {
          showWaiting(state);
        }
      })
      .catch(function () {
        queueHint.textContent = "Нет связи с сервером лаборатории, пробуем снова…";
      });
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target) {
      return;
    }
    var action = target.getAttribute("data-action");

    if (action === "leave") {
      if (!window.confirm("Отменить заявку и закрыть панель?")) {
        return;
      }
      fetch("/api/leave", { method: "POST" }).then(function () {
        window.location.href = "/";
      });
    }

    if (action === "fullscreen") {
      var shell = document.getElementById("panel-view");
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else if (shell.requestFullscreen) {
        shell.requestFullscreen();
      }
    }
  });

  poll();
  window.setInterval(poll, POLL_MS);
})();
