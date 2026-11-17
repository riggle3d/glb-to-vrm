#!/usr/bin/env node
// Riggle API client and CLI: GLB or picture in, talking VRM 1.0 avatar out.
//
// One file, zero dependencies, Node 18+.
//
//   export RIGGLE_API_KEY=rgl_...
//   node riggle.mjs convert model.glb -o model.vrm --preview preview.mp4
//   node riggle.mjs convert --image hero.png -o hero.vrm
//   node riggle.mjs convert --image sheet.png --fix    # redraw a bad picture
//   node riggle.mjs fix-picture sheet.png -o fixed.png  # only the redraw
//   node riggle.mjs balance
//   node riggle.mjs list
//
// As a module:
//
//   import { Riggle } from './riggle.mjs';
//   await new Riggle('rgl_...').convert('model.glb', 'model.vrm');
//
// API reference: https://riggle3d.com/en/guides/vrm-api
// API keys:      https://app.riggle3d.com/developers/

import { randomUUID } from 'node:crypto';
import * as fs from 'node:fs';
import { mkdir, readFile, rename, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';

export const DEFAULT_API_URL = 'https://app.riggle3d.com/api/v1';
const POLL_SECONDS = 4; // the API asks for 3-5 seconds between polls
const DEFAULT_TIMEOUT = 20 * 60; // a model takes 1-3 minutes, a picture 3-6
const USER_AGENT = 'riggle-examples-node/1.0';

const CONTENT_TYPES = {
  '.glb': 'model/gltf-binary',
  '.vrm': 'model/gltf-binary',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
};

const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

/**
 * The API said no, or could not be reached.
 * `status` is the HTTP status (undefined when the request never got an
 * answer), `code` the API's error code (`insufficient_tokens`,
 * `too_many_active`, ...) and `body` the decoded JSON error response.
 */
export class RiggleError extends Error {
  constructor(message, { status, code, body } = {}) {
    super(message);
    this.name = 'RiggleError';
    this.status = status;
    this.code = code;
    this.body = body ?? {};
  }

  /** A field beside `code` inside the error: `required` and `available` on
   *  `insufficient_tokens`, `problems` on `picture_rejected`. */
  detail(key) {
    const error = this.body?.error;
    return error && typeof error === 'object' ? error[key] : undefined;
  }
}

/** The conversion ran and failed. Its tokens were returned. */
export class ConversionFailed extends RiggleError {
  constructor(conversion) {
    super(conversion?.error?.message ?? 'The conversion failed.', {
      code: conversion?.error?.code ?? 'conversion_failed',
    });
    this.name = 'ConversionFailed';
    this.conversion = conversion;
  }
}

/** The redraw ran and no picture came back. Its tokens were returned. */
export class PictureFixFailed extends RiggleError {
  constructor(fix) {
    super(fix?.error?.message ?? 'The picture could not be redrawn.', {
      code: fix?.error?.code ?? 'fix_failed',
    });
    this.name = 'PictureFixFailed';
    this.fix = fix;
  }
}

export class Riggle {
  /**
   * @param {string} [apiKey]  defaults to RIGGLE_API_KEY
   * @param {{baseUrl?: string}} [options]  baseUrl defaults to RIGGLE_API_URL
   */
  constructor(apiKey, { baseUrl } = {}) {
    this.apiKey = apiKey || process.env.RIGGLE_API_KEY || '';
    if (!this.apiKey) {
      throw new RiggleError(
        'No API key. Set RIGGLE_API_KEY or pass one in; keys are made at ' +
          'https://app.riggle3d.com/developers/',
        { code: 'no_api_key' },
      );
    }
    this.baseUrl = (baseUrl || process.env.RIGGLE_API_URL || DEFAULT_API_URL).replace(/\/+$/, '');
  }

  // -- endpoints ------------------------------------------------------------

  /** GET /account: the API token balance and the prices. */
  account() {
    return this.#json('GET', '/account');
  }

  /**
   * POST /conversions: upload a model (`file`) or a picture (`image`).
   * Tokens are taken here and returned automatically if the conversion
   * fails. A retry with the same `idempotencyKey` returns the first
   * conversion instead of making, and charging, a second one.
   *
   * A picture is checked before anything is charged; one that is not a whole,
   * front-facing humanoid is refused with `picture_rejected` (422) and
   * `error.detail('problems')` lists why. `fix: true` has such a picture
   * redrawn first (+2 API tokens, only when it is redrawn); `force: true`
   * skips the check and uses the picture as it is.
   */
  async create({ file, image, name, idempotencyKey, fix, force } = {}) {
    if (Boolean(file) === Boolean(image)) {
      throw new RiggleError('Give either a model file or an image, not both.', {
        code: 'invalid_request',
      });
    }
    if ((fix || force) && !image) {
      throw new RiggleError('fix and force apply to pictures only.', { code: 'invalid_request' });
    }
    const [field, source] = file ? ['file', file] : ['image', image];
    const info = await stat(source).catch(() => null);
    if (!info?.isFile()) {
      throw new RiggleError(`No such file: ${source}`, { code: 'invalid_request' });
    }
    return this.#upload('/conversions', field, source, { name, fix, force }, idempotencyKey);
  }

  /**
   * POST /pictures/fix: start a redraw of a picture as one full-body,
   * front-facing view on a plain background. There is no check first.
   * A job like a conversion: 2 API tokens are taken here and returned if no
   * picture comes back. Resolves with the picture-fix object to poll.
   */
  async createPictureFix(image, { name, idempotencyKey } = {}) {
    const info = await stat(image).catch(() => null);
    if (!info?.isFile()) {
      throw new RiggleError(`No such file: ${image}`, { code: 'invalid_request' });
    }
    return this.#upload('/pictures/fix', 'image', image, { name }, idempotencyKey);
  }

  /** GET /pictures/fix/{id} */
  getPictureFix(id) {
    return this.#json('GET', `/pictures/fix/${encodeURIComponent(id)}`);
  }

  /** GET /pictures/fix/{id}/image: the redrawn PNG, saved to `destination`. */
  downloadFixedPicture(id, destination) {
    return this.#download(`/pictures/fix/${encodeURIComponent(id)}/image`, destination);
  }

  /**
   * Upload a picture, wait for the redraw, save the PNG to `output` (default:
   * the input's name + .fixed.png). Resolves with the finished picture-fix
   * object; the PNG can go straight to `convert({ image })`. Rejects with
   * PictureFixFailed if no picture comes back (the tokens are returned).
   */
  async fixPicture(image, output, { name, onProgress, timeout, poll, idempotencyKey } = {}) {
    const destination = output ?? withExtension(image, '.fixed.png');
    let fix = await this.createPictureFix(image, { name, idempotencyKey });
    onProgress?.(fix, 0);
    if (fix.status === 'processing') {
      fix = await this.#wait(
        (id) => this.getPictureFix(id),
        (id) => `/pictures/fix/${id}/image`,
        fix.id,
        { onProgress, timeout, poll },
      );
    }
    if (fix.status !== 'succeeded') throw new PictureFixFailed(fix);
    await this.downloadFixedPicture(fix.id, destination);
    return fix;
  }

  /** GET /conversions/{id} */
  get(id) {
    return this.#json('GET', `/conversions/${encodeURIComponent(id)}`);
  }

  /** GET /conversions: the latest conversions, newest first (max 100). */
  async list(limit = 20) {
    const data = await this.#json('GET', `/conversions?limit=${Number(limit) || 20}`);
    return data?.data ?? []; // {"object": "list", "data": [...]}
  }

  /** DELETE /conversions/{id}. Deleting a running one does not return its tokens. */
  async delete(id) {
    await this.#json('DELETE', `/conversions/${encodeURIComponent(id)}`);
  }

  /**
   * Poll until the conversion is no longer `processing`; resolve with it.
   * `onProgress(conversion, secondsElapsed)` is called after every poll.
   * Rejects with code `timeout` after `timeout` seconds; the conversion keeps
   * running on the server and can be fetched later.
   */
  wait(id, options) {
    return this.#wait((jobId) => this.get(jobId), (jobId) => `/conversions/${jobId}/vrm`, id, options);
  }

  async #wait(fetchJob, resultRoute, id, { onProgress, timeout, poll } = {}) {
    timeout ??= DEFAULT_TIMEOUT;
    poll ??= POLL_SECONDS;
    const started = Date.now();
    for (;;) {
      const job = await fetchJob(id);
      const elapsed = (Date.now() - started) / 1000;
      onProgress?.(job, elapsed);
      if (job.status !== 'processing') return job;
      if (elapsed + poll > timeout) {
        throw new RiggleError(
          `Still processing after ${Math.round(elapsed)} s. It keeps running on the ` +
            `server; when it is done, download it from ${this.baseUrl}${resultRoute(id)} ` +
            'with the same Authorization header.',
          { code: 'timeout' },
        );
      }
      await sleep(poll);
    }
  }

  /** GET /conversions/{id}/vrm, saved to `destination`. */
  downloadVrm(id, destination) {
    return this.#download(`/conversions/${encodeURIComponent(id)}/vrm`, destination);
  }

  /** GET /conversions/{id}/preview.mp4: every expression played back. */
  downloadPreview(id, destination) {
    return this.#download(`/conversions/${encodeURIComponent(id)}/preview.mp4`, destination);
  }

  /** GET /conversions/{id}/preview/{name}.png; `name` is a key of `preview_images`. */
  downloadPreviewImage(id, name, destination) {
    return this.#download(
      `/conversions/${encodeURIComponent(id)}/preview/${encodeURIComponent(name)}.png`,
      destination,
    );
  }

  // -- the whole thing ------------------------------------------------------

  /**
   * Upload, wait, download. Resolves with the finished conversion object,
   * whose `warnings` say whether the preview deserves a look.
   *
   *   await riggle.convert('model.glb', 'model.vrm');
   *   await riggle.convert({ image: 'hero.png', output: 'hero.vrm', preview: 'hero.mp4' });
   *
   * Rejects with ConversionFailed if the conversion fails (tokens are
   * returned) and RiggleError for anything the API refuses.
   */
  async convert(source, output, options = {}) {
    const opts =
      typeof source === 'object' && source !== null
        ? source
        : { ...options, file: source, output: output ?? options.output };
    const { file, image, name, preview, onProgress, timeout, poll, idempotencyKey, fix, force } = opts;
    const input = file || image;
    const destination = opts.output ?? (input ? withExtension(input, '.vrm') : undefined);

    let conversion = await this.create({ file, image, name, idempotencyKey, fix, force });
    onProgress?.(conversion, 0);
    if (conversion.status === 'processing') {
      conversion = await this.wait(conversion.id, { onProgress, timeout, poll });
    }
    if (conversion.status !== 'succeeded') throw new ConversionFailed(conversion);
    await this.downloadVrm(conversion.id, destination);
    if (preview) await this.downloadPreview(conversion.id, preview);
    return conversion;
  }

  // -- plumbing -------------------------------------------------------------

  async #upload(route, field, source, fields, idempotencyKey) {
    const type = CONTENT_TYPES[path.extname(source).toLowerCase()] ?? 'application/octet-stream';
    // openAsBlob (Node 19.8+) streams from disk; Node 18 reads the file in.
    const blob = fs.openAsBlob
      ? await fs.openAsBlob(source, { type })
      : new Blob([await readFile(source)], { type });
    const key = idempotencyKey ?? randomUUID();

    // The same key on every attempt is what makes this retry safe: if the
    // first upload did arrive, the API hands that job back.
    let last;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt) await sleep(2 * attempt);
      const form = new FormData();
      for (const [name, value] of Object.entries(fields)) {
        if (value) form.set(name, value === true ? 'true' : value);
      }
      form.set(field, blob, path.basename(source));
      try {
        return await this.#json('POST', route, {
          body: form,
          headers: { 'Idempotency-Key': key },
          timeout: 900,
        });
      } catch (error) {
        if (!(error instanceof RiggleError) || error.status !== undefined) throw error;
        last = error; // never got an answer: try again
      }
    }
    throw last;
  }

  async #fetch(method, route, { body, headers, timeout = 60 } = {}) {
    let response;
    try {
      response = await fetch(this.baseUrl + route, {
        method,
        body,
        headers: {
          Authorization: `Bearer ${this.apiKey}`,
          Accept: 'application/json',
          'User-Agent': USER_AGENT,
          ...headers,
        },
        signal: AbortSignal.timeout(timeout * 1000),
      });
    } catch (error) {
      const reason = error?.cause?.message || error?.message || String(error);
      throw new RiggleError(`Could not reach ${this.baseUrl}: ${reason}`, { code: 'network' });
    }
    if (response.ok) return response;

    // Every error is {"error": {"code": ..., "message": ...}}.
    let parsed = {};
    try {
      parsed = JSON.parse(await response.text());
    } catch {
      // not JSON: a proxy or a wrong RIGGLE_API_URL answered
    }
    const error = parsed && typeof parsed === 'object' ? parsed.error : undefined;
    throw new RiggleError(error?.message ?? `HTTP ${response.status} from the Riggle API`, {
      status: response.status,
      code: error?.code,
      body: parsed && typeof parsed === 'object' ? parsed : {},
    });
  }

  // Reads are safe to repeat, so ride out a dropped connection or a gateway
  // hiccup instead of abandoning a conversion that is running.
  async #fetchPatiently(method, route, options) {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.#fetch(method, route, options);
      } catch (error) {
        const transient =
          error instanceof RiggleError &&
          (error.status === undefined || [500, 502, 503, 504].includes(error.status));
        if (!transient || attempt === 3) throw error;
        await sleep(2 * (attempt + 1));
      }
    }
  }

  async #json(method, route, options) {
    const response =
      method === 'GET'
        ? await this.#fetchPatiently(method, route, options)
        : await this.#fetch(method, route, options);
    let text;
    try {
      text = await response.text();
    } catch (error) {
      throw new RiggleError(`Connection lost: ${error.message}`, { code: 'network' });
    }
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      throw new RiggleError(
        `Expected JSON from ${this.baseUrl}${route} and got something else. Is RIGGLE_API_URL right?`,
        { code: 'bad_response' },
      );
    }
  }

  async #download(route, destination) {
    const target = path.resolve(String(destination));
    const partial = `${target}.part`;
    await mkdir(path.dirname(target), { recursive: true });
    const response = await this.#fetchPatiently('GET', route, { timeout: 900 });
    try {
      await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(partial));
    } catch (error) {
      await rm(partial, { force: true });
      throw new RiggleError(`Download failed: ${error.message}`, { code: 'network' });
    }
    await rename(partial, target);
    return destination;
  }
}

function withExtension(file, extension) {
  const parsed = path.parse(String(file));
  return path.join(parsed.dir, parsed.name + extension);
}

// -- command line -------------------------------------------------------------

/** One line that says where the conversion is: rewritten in place on a
 *  terminal, printed once per stage in a log. */
function progressLine(stream = process.stderr) {
  let last;
  let open = false;
  const report = (conversion, elapsed) => {
    const label = conversion.stage ? `${conversion.status}: ${conversion.stage}` : conversion.status;
    const line = `[${String(Math.floor(elapsed)).padStart(4)}s] ${label}`;
    if (stream.isTTY) {
      stream.write(`\r\x1b[K${line}`);
      open = true;
    } else if (label !== last) {
      stream.write(`${line}\n`);
    }
    last = label;
  };
  report.done = () => {
    if (open) stream.write('\n');
    open = false;
  };
  return report;
}

/** An API error as something a person can act on. */
function explain(error) {
  const quiet = ['network', 'timeout', 'no_api_key'];
  const lines = [
    error.code && !quiet.includes(error.code)
      ? `Error: ${error.message} [${error.code}]${error.status ? ` (HTTP ${error.status})` : ''}`
      : `Error: ${error.message}`,
  ];
  if (error.code === 'insufficient_tokens') {
    const required = error.detail('required');
    const available = error.detail('available');
    if (required != null && available != null) {
      lines.push(`This needs ${required} API tokens; the account has ${available}.`);
    }
    lines.push('Buy API tokens at https://app.riggle3d.com/developers/');
  } else if (error.code === 'picture_rejected') {
    for (const problem of error.detail('problems') ?? []) {
      lines.push(`  - ${problem.message ?? problem.code}`);
    }
    lines.push(
      'Nothing was charged. Send another picture, or this one again with --fix ' +
        '(redraw it first, +2 API tokens) or --force (use it as it is).',
    );
  } else if (error.code === 'too_many_active') {
    lines.push('Wait for a running job to finish, then try again.');
  } else if (error.code === 'unauthorized') {
    lines.push(
      'Check RIGGLE_API_KEY; keys are made and revoked at https://app.riggle3d.com/developers/',
    );
  }
  return lines.join('\n');
}

const USAGE = `Turn a GLB/VRM model or a picture into a talking VRM 1.0 avatar with the Riggle API.

Usage:
  riggle.mjs convert MODEL [-o OUT.vrm] [--preview OUT.mp4] [--name NAME] [--timeout SECONDS]
  riggle.mjs convert --image PICTURE [--fix | --force] [-o OUT.vrm] [--preview OUT.mp4]
  riggle.mjs fix-picture PICTURE [-o OUT.png] [--timeout SECONDS]
  riggle.mjs balance
  riggle.mjs list [--limit N]

  MODEL    a .glb or .vrm, rigged or not (10 API tokens)
  PICTURE  a .png or .jpg of one whole character, standing, facing the camera (25 API tokens)
  --fix    if the picture check finds a problem, redraw the picture first (+2 API tokens)
  --force  skip the picture check and use the picture as it is

  fix-picture redraws a picture as one full-body front view (2 API tokens)
  and saves the PNG; nothing is converted.

Environment:
  RIGGLE_API_KEY  required; made at https://app.riggle3d.com/developers/
  RIGGLE_API_URL  default ${DEFAULT_API_URL}

Reference: https://riggle3d.com/en/guides/vrm-api
`;

function parseArgs(argv) {
  const takesValue = {
    '-o': 'output',
    '--output': 'output',
    '--image': 'image',
    '--preview': 'preview',
    '--name': 'name',
    '--timeout': 'timeout',
    '--limit': 'limit',
  };
  const args = { positional: [] };
  for (let i = 0; i < argv.length; i++) {
    let arg = argv[i];
    let value;
    if (arg.startsWith('--') && arg.includes('=')) {
      value = arg.slice(arg.indexOf('=') + 1);
      arg = arg.slice(0, arg.indexOf('='));
    }
    if (arg === '-h' || arg === '--help') {
      args.help = true;
    } else if (arg === '--fix' || arg === '--force') {
      args[arg.slice(2)] = true;
    } else if (arg in takesValue) {
      value ??= argv[++i];
      if (value === undefined) throw new UsageError(`${arg} needs a value`);
      args[takesValue[arg]] = value;
    } else if (arg.startsWith('-') && arg !== '-') {
      throw new UsageError(`Unknown option ${arg}`);
    } else {
      args.positional.push(arg);
    }
  }
  return args;
}

class UsageError extends Error {}

async function cmdConvert(client, args) {
  const file = args.positional[0];
  if (Boolean(file) === Boolean(args.image) || args.positional.length > 1) {
    throw new UsageError('Give a model file, or --image with a picture (not both).');
  }
  if ((args.fix || args.force) && !args.image) {
    throw new UsageError('--fix and --force apply to --image only.');
  }
  const source = file || args.image;
  const output = args.output ?? withExtension(source, '.vrm');
  const timeout = args.timeout === undefined ? DEFAULT_TIMEOUT : Number(args.timeout);
  if (!Number.isFinite(timeout) || timeout <= 0) throw new UsageError('--timeout takes seconds');

  const progress = progressLine();
  console.error(`Uploading ${source}`);
  let conversion;
  try {
    conversion = await client.convert({
      file,
      image: args.image,
      output,
      name: args.name,
      preview: args.preview,
      onProgress: progress,
      timeout,
      fix: args.fix,
      force: args.force,
    });
  } catch (error) {
    progress.done();
    if (!(error instanceof ConversionFailed)) throw error;
    console.error(`Conversion ${error.conversion?.id} failed: ${error.message}`);
    if (error.conversion?.refunded) {
      console.error(`Its ${error.conversion.cost} API tokens were returned.`);
    }
    return 1;
  }
  progress.done();
  const warnings = conversion.warnings ?? [];
  for (const warning of warnings) console.error(`Warning: ${warning}`);
  if (warnings.length && !args.preview) {
    console.error('Look at the preview before shipping it: add --preview preview.mp4');
  }
  console.error(`Saved ${output} (conversion ${conversion.id}, ${conversion.cost} API tokens)`);
  if (args.preview) console.error(`Saved ${args.preview}`);
  return 0;
}

async function cmdFixPicture(client, args) {
  const image = args.positional[0];
  if (!image || args.positional.length > 1) {
    throw new UsageError('fix-picture takes one picture.');
  }
  const output = args.output ?? withExtension(image, '.fixed.png');
  const timeout = args.timeout === undefined ? DEFAULT_TIMEOUT : Number(args.timeout);
  if (!Number.isFinite(timeout) || timeout <= 0) throw new UsageError('--timeout takes seconds');
  const progress = progressLine();
  console.error(`Uploading ${image}`);
  let fix;
  try {
    fix = await client.fixPicture(image, output, { onProgress: progress, timeout });
  } catch (error) {
    progress.done();
    if (!(error instanceof PictureFixFailed)) throw error;
    console.error(`Picture fix ${error.fix?.id} failed: ${error.message}`);
    if (error.fix?.refunded) console.error(`Its ${error.fix.cost} API tokens were returned.`);
    return 1;
  }
  progress.done();
  console.error(`Saved ${output} (picture fix ${fix.id}, ${fix.cost} API tokens)`);
  return 0;
}

async function cmdBalance(client) {
  const account = await client.account();
  console.log(`API tokens: ${account.api_tokens}`);
  for (const [action, cost] of Object.entries(account.prices ?? {})) {
    console.log(`  ${action}: ${cost}`);
  }
  if (account.max_active_conversions != null) {
    console.log(`Conversions in flight at once: ${account.max_active_conversions}`);
  }
  return 0;
}

async function cmdList(client, args) {
  const limit = args.limit === undefined ? 20 : Number(args.limit);
  if (!Number.isInteger(limit) || limit < 1) throw new UsageError('--limit takes a number');
  const conversions = await client.list(limit);
  if (!conversions.length) {
    console.log('No conversions yet.');
    return 0;
  }
  const row = (id, status, stage, input, cost, created, name) =>
    [
      String(id).padEnd(36),
      String(status).padEnd(10),
      String(stage).padEnd(16),
      String(input).padEnd(5),
      String(cost).padStart(4),
      String(created).padEnd(20),
      name,
    ].join('  ');
  console.log(row('ID', 'STATUS', 'STAGE', 'INPUT', 'COST', 'CREATED', 'NAME'));
  let refunded = false;
  for (const c of conversions) {
    const returned = c.status === 'failed' && c.refunded;
    refunded ||= returned;
    console.log(
      row(
        c.id ?? '',
        returned ? 'failed*' : (c.status ?? ''),
        c.stage ?? '-',
        c.input ?? '',
        c.cost ?? '',
        c.created_at ?? '',
        c.name ?? '',
      ),
    );
  }
  if (refunded) console.log('* tokens returned');
  return 0;
}

export async function main(argv = process.argv.slice(2)) {
  const commands = {
      convert: cmdConvert,
      'fix-picture': cmdFixPicture,
      balance: cmdBalance,
      list: cmdList,
    };
  try {
    const [command, ...rest] = argv;
    const args = parseArgs(rest);
    if (!command || command === '-h' || command === '--help' || args.help) {
      process.stdout.write(USAGE);
      return command ? 0 : 2;
    }
    if (!(command in commands)) throw new UsageError(`Unknown command ${command}`);
    return await commands[command](new Riggle(), args);
  } catch (error) {
    if (error instanceof UsageError) {
      console.error(`${error.message}\n\n${USAGE}`);
      return 2;
    }
    if (error instanceof RiggleError) {
      console.error(explain(error));
      return 1;
    }
    throw error;
  }
}

function invokedDirectly() {
  try {
    return (
      Boolean(process.argv[1]) &&
      fs.realpathSync(process.argv[1]) === fs.realpathSync(fileURLToPath(import.meta.url))
    );
  } catch {
    return false;
  }
}

if (invokedDirectly()) {
  process.exitCode = await main();
}
