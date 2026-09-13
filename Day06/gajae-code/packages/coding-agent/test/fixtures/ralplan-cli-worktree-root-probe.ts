import { runRalplanCliCommand } from "../../src/commands/ralplan";

const result = await runRalplanCliCommand(process.argv.slice(2), process.cwd());
if (result.stdout) process.stdout.write(result.stdout);
if (result.stderr) process.stderr.write(result.stderr);
process.exit(result.status);
