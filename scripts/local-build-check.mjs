import { readFile } from "node:fs/promises";
import path from "node:path";

const DASHBOARD_PATH = path.resolve(process.cwd(), "templates/index.html");

async function main() {
  const html = await readFile(DASHBOARD_PATH, "utf8");
  const scriptBlocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (!scriptBlocks.length) {
    throw new Error(`No inline <script> blocks found in ${DASHBOARD_PATH}`);
  }

  scriptBlocks.forEach((block, index) => {
    const script = block[1];
    try {
      new Function(script);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      throw new Error(`Inline script #${index + 1} syntax error: ${message}`);
    }
  });

  console.log(`OK: validated ${scriptBlocks.length} inline script block(s) in templates/index.html`);
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`Build check failed: ${message}`);
  process.exit(1);
});
