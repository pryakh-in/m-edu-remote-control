/* Угол обзора: MJPEG-прокси камеры лаборатории. */
(function () {
  "use strict";

  var dock = document.getElementById("camera-dock");
  var frame = document.getElementById("camera-frame");
  var toggle = document.getElementById("camera-toggle");
  var errorBox = document.getElementById("camera-error");
  if (!dock || !frame) {
    return;
  }

  var collapsed = window.localStorage.getItem("medu_camera_collapsed") === "1";

  function applyCollapsed() {
    dock.classList.toggle("collapsed", collapsed);
    toggle.textContent = collapsed ? "показать" : "свернуть";
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (!collapsed && !frame.getAttribute("src")) {
      frame.src = "/camera/mjpeg?t=" + Date.now();
    }
  }

  fetch("/camera/status", { headers: { Accept: "application/json" }, credentials: "same-origin" })
    .then(function (response) {
      if (!response.ok) {
        return null;
      }
      return response.json();
    })
    .then(function (status) {
      if (!status || !status.enabled) {
        return;
      }
      dock.hidden = false;
      applyCollapsed();
      if (!status.ffmpeg) {
        errorBox.hidden = false;
        errorBox.textContent = "На сервере нет ffmpeg";
      }
    })
    .catch(function () {
      /* камера опциональна */
    });

  toggle.addEventListener("click", function () {
    collapsed = !collapsed;
    window.localStorage.setItem("medu_camera_collapsed", collapsed ? "1" : "0");
    applyCollapsed();
  });

  frame.addEventListener("error", function () {
    errorBox.hidden = false;
    window.setTimeout(function () {
      if (!collapsed) {
        frame.src = "/camera/mjpeg?t=" + Date.now();
      }
    }, 4000);
  });

  frame.addEventListener("load", function () {
    errorBox.hidden = true;
  });
})();
