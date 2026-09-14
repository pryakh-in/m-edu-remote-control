/* Редактор реестра роботов. */
(function () {
  "use strict";

  var textarea = document.getElementById("robots");
  var message = document.getElementById("message");
  var original = textarea.value;

  function show(text, isError) {
    message.hidden = false;
    message.className = isError ? "alert" : "alert alert-info";
    message.textContent = text;
  }

  document.getElementById("save").addEventListener("click", function () {
    var parsed;
    try {
      parsed = JSON.parse(textarea.value);
    } catch (error) {
      show("Некорректный JSON: " + error.message, true);
      return;
    }
    if (!Array.isArray(parsed) || !parsed.length) {
      show("Ожидается непустой список роботов.", true);
      return;
    }

    fetch("/admin/api/robots", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ robots: parsed }),
    })
      .then(function (response) {
        if (response.status === 401) {
          window.location.href = "/admin/login";
          return null;
        }
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(
              typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)
            );
          }
          return data;
        });
      })
      .then(function (data) {
        if (data) {
          original = textarea.value;
          show("Сохранено роботов: " + data.count + ". Опрос состояния выполнен.", false);
        }
      })
      .catch(function (error) {
        show(error.message, true);
      });
  });

  document.getElementById("reload").addEventListener("click", function () {
    textarea.value = original;
    message.hidden = true;
  });
})();
