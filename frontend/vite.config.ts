import react from "@vitejs/plugin-react";
import {defineConfig, loadEnv} from "vite";

export default defineConfig(({mode}) => {
    const environment = loadEnv(mode, ".", "");
    const backendUrl = environment.OWL_BACKEND_URL || "http://127.0.0.1:8000";
    const proxyWithOrigin = {
        target: backendUrl,
        changeOrigin: true,
        headers: {origin: backendUrl},
    };

    return {
        plugins: [react()],
        base: "/static/",
        build: {
            outDir: "dist",
            emptyOutDir: true,
            rollupOptions: {
                input: "src/main.tsx",
                output: {
                    entryFileNames: "owl-frontend.js",
                    chunkFileNames: "chunks/[name]-[hash].js",
                    assetFileNames: "owl-frontend[extname]",
                },
            },
        },
        server: {
            proxy: {
                "/bitbucket": proxyWithOrigin,
                "/bookmarks": proxyWithOrigin,
                "/home": {
                    target: backendUrl,
                    changeOrigin: true,
                },
            },
        },
        test: {
            environment: "jsdom",
            setupFiles: "./src/test/setup.ts",
        },
    };
});
