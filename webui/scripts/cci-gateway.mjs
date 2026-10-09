import http from "node:http";

const port = Number(process.env.PORT ?? 8001);
const dashboardPort = Number(process.env.DASHBOARD_PORT ?? 8002);
const muralPort = Number(process.env.MURAL_PORT ?? 3102);
const studioPort = Number(process.env.STUDIO_PORT ?? 8010);
const studioPrefix = (process.env.STUDIO_BASE_PATH ?? "/studio").replace(/\/+$/, "");
if (!/^(?:\/[A-Za-z0-9_-]+)+$/.test(studioPrefix)) {
  throw new Error("STUDIO_BASE_PATH must be a nonempty URL prefix");
}
const hopHeaders = ["connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade"];

function endToEndHeaders(headers) {
  const result = { ...headers };
  for (const name of [...hopHeaders, ...String(headers.connection ?? "").split(",")]) {
    delete result[name.trim().toLowerCase()];
  }
  return result;
}

// 根路径归旧看板；论文网站保留前缀，Studio 去掉前缀后进入独立服务。
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://localhost");
  if (url.pathname === studioPrefix) {
    response.writeHead(308, { location: `${studioPrefix}/${url.search}` });
    response.end();
    return;
  }
  const isStudio = url.pathname.startsWith(`${studioPrefix}/`);
  const isMural = url.pathname === "/mural-presenter" || url.pathname.startsWith("/mural-presenter/");
  const upstream = http.request({
    hostname: "127.0.0.1",
    port: isStudio ? studioPort : isMural ? muralPort : dashboardPort,
    path: isStudio ? request.url.slice(studioPrefix.length) : request.url,
    method: request.method,
    headers: endToEndHeaders(request.headers),
  }, (incoming) => {
    const headers = endToEndHeaders(incoming.headers);
    if (isMural && url.pathname.endsWith(".pdf") && incoming.statusCode < 400) {
      headers["content-type"] = "application/pdf";
    }
    response.writeHead(incoming.statusCode, headers);
    incoming.pipe(response);
    incoming.on("error", () => response.destroy());
  });
  upstream.on("error", (error) => {
    console.error("Upstream request failed:", error.message);
    if (!response.headersSent) response.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
    response.end("Upstream service unavailable");
  });
  response.on("close", () => upstream.destroy());
  request.pipe(upstream);
});
server.listen(port, "0.0.0.0", () => {
  console.log(`CCI gateway :${server.address().port}: / -> :${dashboardPort}, /mural-presenter -> :${muralPort}, ${studioPrefix} -> :${studioPort}`);
});
