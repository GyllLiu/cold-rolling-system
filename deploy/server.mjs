import { startProdServer } from "vinext/server/prod-server";
import { fileURLToPath } from "node:url";

await startProdServer({
  host: "127.0.0.1",
  port: Number(process.env.PORT || 3100),
  outDir: fileURLToPath(new URL("../dist", import.meta.url)),
});
