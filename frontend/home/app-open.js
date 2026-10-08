"use strict";
// Counts one visit each time an app's page is opened: <script src="/home/app-open.js" data-app="naas">.
(() => {
  const app = document.currentScript?.dataset.app;
  if (!app) return;
  fetch("/api/home/opened", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({app})}).catch(() => {});
})();
