import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./styles.css";

// BrowserRouter (not hash routing): every surface has a real path (/jobs,
// /dashboards/<id>, /c/<id>) that the backend must serve index.html for — the
// SPA fallback in backend/app/main.py is what makes deep links work in prod.
createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
