import { Defuddle } from "defuddle/node";
import { parseHTML } from "linkedom";

const chunks = [];
for await (const chunk of process.stdin) {
  chunks.push(chunk);
}
const { html, url } = JSON.parse(Buffer.concat(chunks).toString("utf8"));
const { document } = parseHTML(html);
const result = await Defuddle(document, url, { markdown: true, useAsync: false });
process.stdout.write(JSON.stringify({ title: result.title || "", content: result.content || "" }));
