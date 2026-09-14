/*
 * Мост между панелью робота и прокси лаборатории.
 *
 * Панель Promobot M Edu подключается к роботу напрямую: rosbridge по
 * ws://<hostname страницы>:9090 и REST по http://<host>:8081. Внутри iframe
 * лаборатории таких портов нет, поэтому все исходящие соединения панели
 * переписываются на прокси того же origin: BASE/__ws/<порт> и BASE/__rest/...
 * Так адреса роботов не попадают в браузер ученика.
 */
(function () {
  "use strict";

  var BASE = window.__PANEL_BASE__;
  if (!BASE) {
    return;
  }

  var wsScheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  var pageOrigin = window.location.origin;
  var REST_PORTS = ["8081"];

  function alreadyProxied(pathname) {
    return pathname.indexOf(BASE + "/") === 0;
  }

  function toProxyWs(input) {
    try {
      var url = new URL(String(input), window.location.href);
      if (url.protocol !== "ws:" && url.protocol !== "wss:") {
        return input;
      }
      if (alreadyProxied(url.pathname)) {
        return input;
      }
      var port = url.port || (url.protocol === "wss:" ? "443" : "80");
      return wsScheme + "//" + window.location.host + BASE + "/__ws/" + port + url.search;
    } catch (error) {
      return input;
    }
  }

  function toProxyHttp(input) {
    if (typeof input !== "string" || input === "" || input.charAt(0) === "#") {
      return input;
    }
    if (/^(data|blob|javascript|mailto):/i.test(input)) {
      return input;
    }
    try {
      var url = new URL(input, window.location.href);
      if (REST_PORTS.indexOf(url.port) !== -1) {
        return BASE + "/__rest" + url.pathname + url.search + url.hash;
      }
      if (url.origin !== pageOrigin) {
        return input;
      }
      if (alreadyProxied(url.pathname)) {
        return input;
      }
      return BASE + url.pathname + url.search + url.hash;
    } catch (error) {
      return input;
    }
  }

  var NativeWebSocket = window.WebSocket;
  if (NativeWebSocket) {
    var ProxiedWebSocket = function (url, protocols) {
      return protocols === undefined
        ? new NativeWebSocket(toProxyWs(url))
        : new NativeWebSocket(toProxyWs(url), protocols);
    };
    ProxiedWebSocket.prototype = NativeWebSocket.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (key) {
      ProxiedWebSocket[key] = NativeWebSocket[key];
    });
    window.WebSocket = ProxiedWebSocket;
  }

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (resource, init) {
      if (typeof resource === "string") {
        return nativeFetch.call(this, toProxyHttp(resource), init);
      }
      if (resource && typeof resource.url === "string") {
        var rewritten = toProxyHttp(resource.url);
        if (rewritten !== resource.url) {
          return nativeFetch.call(this, new Request(rewritten, resource), init);
        }
      }
      return nativeFetch.call(this, resource, init);
    };
  }

  var nativeOpen = window.XMLHttpRequest && window.XMLHttpRequest.prototype.open;
  if (nativeOpen) {
    window.XMLHttpRequest.prototype.open = function (method, url) {
      var args = Array.prototype.slice.call(arguments);
      args[1] = toProxyHttp(url);
      return nativeOpen.apply(this, args);
    };
  }

  var NativeEventSource = window.EventSource;
  if (NativeEventSource) {
    var ProxiedEventSource = function (url, config) {
      return new NativeEventSource(toProxyHttp(url), config);
    };
    ProxiedEventSource.prototype = NativeEventSource.prototype;
    window.EventSource = ProxiedEventSource;
  }

  // Картинки и аудио панель ставит напрямую через src="/images/...".
  var patchedAttributes = { src: true, href: true };
  var nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (typeof name === "string" && patchedAttributes[name.toLowerCase()]) {
      return nativeSetAttribute.call(this, name, toProxyHttp(value));
    }
    return nativeSetAttribute.call(this, name, value);
  };

  ["HTMLImageElement", "HTMLScriptElement", "HTMLAudioElement", "HTMLSourceElement"].forEach(
    function (name) {
      var ctor = window[name];
      if (!ctor) {
        return;
      }
      var descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, "src");
      if (!descriptor || !descriptor.set) {
        return;
      }
      Object.defineProperty(ctor.prototype, "src", {
        configurable: true,
        enumerable: descriptor.enumerable,
        get: descriptor.get,
        set: function (value) {
          descriptor.set.call(this, toProxyHttp(value));
        },
      });
    }
  );

  // Ученик работает в iframe: сообщаем родителю о готовности панели.
  window.addEventListener("load", function () {
    try {
      window.parent.postMessage({ source: "medu-panel", event: "loaded" }, pageOrigin);
    } catch (error) {
      /* родителя может не быть */
    }
  });
})();
