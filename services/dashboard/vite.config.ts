import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a gateway on :8080 (e.g. kubectl port-forward).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8080", "/mcp": "http://127.0.0.1:8080" } },
});
