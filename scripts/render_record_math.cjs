// Offline static MathML: input and output are JSON over stdio, never shell text.
const fs = require('node:fs');
const katex = require('./vendor/katex/katex.min.js');
try {
  const expressions = JSON.parse(fs.readFileSync(0, 'utf8'));
  const rendered = expressions.map(({tex, display, isolateSourceErrors = false}) => {
    try {
      const mathml = katex.renderToString(tex, {
        output: 'mathml', displayMode: display, throwOnError: true,
        trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 40,
      });
      // Existing long-lived Python runners still consume the original strings.
      return isolateSourceErrors ? {status: 'rendered', mathml} : mathml;
    } catch (error) {
      // A malformed formula is source data, not a failure of the whole page.
      // Other failures still terminate the renderer rather than hiding them.
      if (!(error instanceof katex.ParseError) || !isolateSourceErrors) throw error;
      return {status: 'source_error', error: String(error.message)};
    }
  });
  process.stdout.write(JSON.stringify(rendered));
} catch (error) {
  process.stderr.write(String(error.message));
  process.exitCode = 1;
}
