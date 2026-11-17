#!/usr/bin/env python3
"""Riggle API client and CLI: GLB or picture in, talking VRM 1.0 avatar out.

One file, standard library only, Python 3.9+.

    export RIGGLE_API_KEY=rgl_...
    python riggle.py convert model.glb -o model.vrm --preview preview.mp4
    python riggle.py convert --image hero.png -o hero.vrm
    python riggle.py convert --image sheet.png --fix    # redraw a bad picture
    python riggle.py fix-picture sheet.png -o fixed.png  # only the redraw
    python riggle.py balance
    python riggle.py list

As a module:

    from riggle import Riggle
    Riggle("rgl_...").convert("model.glb", "model.vrm")

API reference: https://riggle3d.com/en/guides/vrm-api
API keys:      https://app.riggle3d.com/developers/
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

__all__ = ["Riggle", "RiggleError", "ConversionFailed", "PictureFixFailed",
           "DEFAULT_API_URL"]

DEFAULT_API_URL = "https://app.riggle3d.com/api/v1"
POLL_SECONDS = 4          # the API asks for 3-5 seconds between polls
DEFAULT_TIMEOUT = 20 * 60  # a model takes 1-3 minutes, a picture 3-6
USER_AGENT = "riggle-examples-python/1.0"

_CHUNK = 1024 * 1024
_CONTENT_TYPES = {
    ".glb": "model/gltf-binary",
    ".vrm": "model/gltf-binary",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class RiggleError(Exception):
    """The API said no, or could not be reached.

    `status` is the HTTP status (None when the request never got an answer),
    `code` the API's error code (`insufficient_tokens`, `too_many_active`, ...)
    and `body` the decoded JSON error response, if there was one.
    """

    def __init__(self, message, status=None, code=None, body=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body or {}

    def detail(self, key):
        """A field beside `code` inside the error: `required` and `available`
        on `insufficient_tokens`, `problems` on `picture_rejected`."""
        error = self.body.get("error")
        return error.get(key) if isinstance(error, dict) else None


class ConversionFailed(RiggleError):
    """The conversion ran and failed. Its tokens were returned."""

    def __init__(self, conversion):
        error = conversion.get("error") or {}
        super().__init__(
            error.get("message") or "The conversion failed.",
            code=error.get("code") or "conversion_failed",
        )
        self.conversion = conversion


class PictureFixFailed(RiggleError):
    """The redraw ran and no picture came back. Its tokens were returned."""

    def __init__(self, fix):
        error = fix.get("error") or {}
        super().__init__(
            error.get("message") or "The picture could not be redrawn.",
            code=error.get("code") or "fix_failed",
        )
        self.fix = fix


class Riggle:
    """A small client for the Riggle API.

        client = Riggle()                       # reads RIGGLE_API_KEY
        client.convert("model.glb", "model.vrm")
        client.convert(image="hero.png", output="hero.vrm")
    """

    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key or os.environ.get("RIGGLE_API_KEY", "")
        if not self.api_key:
            raise RiggleError(
                "No API key. Set RIGGLE_API_KEY or pass one in; keys are made "
                "at https://app.riggle3d.com/developers/",
                code="no_api_key",
            )
        self.base_url = (
            base_url or os.environ.get("RIGGLE_API_URL") or DEFAULT_API_URL
        ).rstrip("/")

    # -- endpoints ---------------------------------------------------------

    def account(self):
        """GET /account: the API token balance and the prices."""
        return self._json("GET", "/account")

    def create(self, file=None, image=None, name=None, idempotency_key=None,
               fix=False, force=False):
        """POST /conversions: upload a model (`file`) or a picture (`image`).

        A picture is checked before anything is charged; one that is not a
        whole, front-facing humanoid is refused with `picture_rejected` (422)
        and `exc.detail("problems")` lists why. `fix=True` has such a picture
        redrawn first (+2 API tokens, only when it is redrawn); `force=True`
        skips the check and uses the picture as it is.

        Tokens are taken here and returned automatically if the conversion
        fails. A retry with the same `idempotency_key` returns the first
        conversion instead of making, and charging, a second one.
        """
        if bool(file) == bool(image):
            raise RiggleError(
                "Give either a model file or an image, not both.",
                code="invalid_request",
            )
        field, path = ("file", file) if file else ("image", image)
        if not os.path.isfile(path):
            raise RiggleError("No such file: %s" % path, code="invalid_request")
        if (fix or force) and not image:
            raise RiggleError(
                "fix and force apply to pictures only.", code="invalid_request")
        fields = {"name": name} if name else {}
        if fix:
            fields["fix"] = "true"
        if force:
            fields["force"] = "true"
        return self._upload("/conversions", fields, field, path, idempotency_key)

    def create_picture_fix(self, image, name=None, idempotency_key=None):
        """POST /pictures/fix: start a redraw of a picture as one full-body,
        front-facing view on a plain background. There is no check first.

        A job like a conversion: 2 API tokens are taken here and returned if
        no picture comes back. Returns the picture-fix object to poll.
        """
        if not os.path.isfile(image):
            raise RiggleError("No such file: %s" % image, code="invalid_request")
        fields = {"name": name} if name else {}
        return self._upload(
            "/pictures/fix", fields, "image", image, idempotency_key)

    def get_picture_fix(self, fix_id):
        """GET /pictures/fix/{id}"""
        return self._json("GET", "/pictures/fix/%s" % _quote(fix_id))

    def download_fixed_picture(self, fix_id, path):
        """GET /pictures/fix/{id}/image: the redrawn PNG, saved to `path`."""
        return self._download("/pictures/fix/%s/image" % _quote(fix_id), path)

    def fix_picture(self, image, output=None, *, name=None, on_progress=None,
                    timeout=DEFAULT_TIMEOUT, poll=POLL_SECONDS,
                    idempotency_key=None):
        """Upload a picture, wait for the redraw, save the PNG to `output`
        (default: the input's name + .fixed.png).

        Returns the finished picture-fix object. The PNG can go straight to
        `convert(image=...)`. Raises PictureFixFailed if no picture comes back
        (the tokens are returned).
        """
        if output is None:
            output = os.path.splitext(str(image))[0] + ".fixed.png"
        fix = self.create_picture_fix(
            image, name=name, idempotency_key=idempotency_key)
        if on_progress:
            on_progress(fix, 0)
        if fix.get("status") == "processing":
            fix = self._wait(self.get_picture_fix, "/pictures/fix/%s/image",
                             fix["id"], on_progress, timeout, poll)
        if fix.get("status") != "succeeded":
            raise PictureFixFailed(fix)
        self.download_fixed_picture(fix["id"], output)
        return fix

    def get(self, conversion_id):
        """GET /conversions/{id}"""
        return self._json("GET", "/conversions/%s" % _quote(conversion_id))

    def list(self, limit=20):
        """GET /conversions: the latest conversions, newest first (max 100)."""
        data = self._json("GET", "/conversions?limit=%d" % int(limit))
        return data.get("data") or []  # {"object": "list", "data": [...]}

    def delete(self, conversion_id):
        """DELETE /conversions/{id}. Deleting one that is still running does
        not return its tokens."""
        self._json("DELETE", "/conversions/%s" % _quote(conversion_id))

    def wait(self, conversion_id, on_progress=None, timeout=DEFAULT_TIMEOUT,
             poll=POLL_SECONDS):
        """Poll until the conversion is no longer `processing`; return it.

        `on_progress(conversion, seconds_elapsed)` is called after every poll.
        Raises RiggleError(code="timeout") when `timeout` seconds pass; the
        conversion keeps running on the server and can be fetched later.
        """
        return self._wait(self.get, "/conversions/%s/vrm", conversion_id,
                          on_progress, timeout, poll)

    def _wait(self, fetch, result_route, job_id, on_progress, timeout, poll):
        started = time.monotonic()
        while True:
            job = fetch(job_id)
            elapsed = time.monotonic() - started
            if on_progress:
                on_progress(job, elapsed)
            if job.get("status") != "processing":
                return job
            if elapsed + poll > timeout:
                raise RiggleError(
                    "Still processing after %d s. It keeps running on the "
                    "server; when it is done, download it from %s%s with the "
                    "same Authorization header."
                    % (elapsed, self.base_url, result_route % job_id),
                    code="timeout",
                )
            time.sleep(poll)

    def download_vrm(self, conversion_id, path):
        """GET /conversions/{id}/vrm, saved to `path`."""
        return self._download("/conversions/%s/vrm" % _quote(conversion_id), path)

    def download_preview(self, conversion_id, path):
        """GET /conversions/{id}/preview.mp4: every expression played back."""
        return self._download(
            "/conversions/%s/preview.mp4" % _quote(conversion_id), path)

    def download_preview_image(self, conversion_id, name, path):
        """GET /conversions/{id}/preview/{name}.png: one still. `name` is a
        key of the conversion's `preview_images`."""
        return self._download(
            "/conversions/%s/preview/%s.png"
            % (_quote(conversion_id), _quote(name)), path)

    # -- the whole thing ---------------------------------------------------

    def convert(self, file=None, output=None, *, image=None, name=None,
                preview=None, on_progress=None, timeout=DEFAULT_TIMEOUT,
                poll=POLL_SECONDS, idempotency_key=None, fix=False,
                force=False):
        """Upload, wait, download. Returns the finished conversion object,
        whose `warnings` say whether the preview deserves a look.

        Raises ConversionFailed if the conversion fails (tokens are returned)
        and RiggleError for anything the API refuses.
        """
        source = file or image
        if output is None and source:
            output = os.path.splitext(str(source))[0] + ".vrm"
        conversion = self.create(
            file=file, image=image, name=name, idempotency_key=idempotency_key,
            fix=fix, force=force)
        if on_progress:
            on_progress(conversion, 0)
        if conversion.get("status") == "processing":
            conversion = self.wait(
                conversion["id"], on_progress=on_progress, timeout=timeout,
                poll=poll)
        if conversion.get("status") != "succeeded":
            raise ConversionFailed(conversion)
        self.download_vrm(conversion["id"], output)
        if preview:
            self.download_preview(conversion["id"], preview)
        return conversion

    # -- plumbing ----------------------------------------------------------

    def _upload(self, route, fields, file_field, path, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        # The same key on every attempt is what makes this retry safe: if the
        # first upload did arrive, the API hands that job back.
        last = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            body, length, content_type = _multipart(fields, file_field, path)
            try:
                return self._json(
                    "POST", route, body=body, timeout=900,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(length),
                        "Idempotency-Key": key,
                    },
                )
            except RiggleError as exc:
                if exc.status is not None:
                    raise  # the API answered; its answer stands
                last = exc
        raise last

    def _open(self, method, path, headers=None, body=None, timeout=60):
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method)
        request.add_header("Authorization", "Bearer " + self.api_key)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise _error_from(exc.code, exc.read()) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise RiggleError(
                "Could not reach %s: %s" % (self.base_url, reason),
                code="network",
            ) from None

    def _open_patiently(self, method, path, **kwargs):
        """Reads are safe to repeat, so ride out a dropped connection or a
        gateway hiccup instead of abandoning a conversion that is running."""
        for attempt in range(4):
            try:
                return self._open(method, path, **kwargs)
            except RiggleError as exc:
                transient = exc.status is None or exc.status in (500, 502, 503, 504)
                if not transient or attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    def _json(self, method, path, **kwargs):
        opener = self._open_patiently if method == "GET" else self._open
        try:
            with opener(method, path, **kwargs) as response:
                raw = response.read()
        except OSError as exc:
            raise RiggleError(
                "Connection lost: %s" % exc, code="network") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise RiggleError(
                "Expected JSON from %s%s and got something else. Is "
                "RIGGLE_API_URL right?" % (self.base_url, path),
                code="bad_response",
            ) from None

    def _download(self, path, destination):
        destination = str(destination)
        partial = destination + ".part"
        folder = os.path.dirname(os.path.abspath(destination))
        os.makedirs(folder, exist_ok=True)
        try:
            with self._open_patiently("GET", path, timeout=300) as response, \
                    open(partial, "wb") as out:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
        except OSError as exc:
            _remove(partial)
            raise RiggleError(
                "Download failed: %s" % exc, code="network") from None
        except BaseException:
            _remove(partial)
            raise
        os.replace(partial, destination)
        return destination


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _error_from(status, raw):
    """Turn `{"error": {"code": ..., "message": ...}}` into a RiggleError."""
    body, code, message = {}, None, None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            body = parsed
            error = parsed.get("error")
            if isinstance(error, dict):
                code, message = error.get("code"), error.get("message")
    except ValueError:
        pass
    return RiggleError(
        message or "HTTP %s from the Riggle API" % status,
        status=status, code=code, body=body)


def _multipart(fields, file_field, path):
    """multipart/form-data by hand. The file is streamed from disk in 1 MB
    pieces, so a 100 MB model is never held in memory.

    Returns (body iterator, content length, content type).
    """
    boundary = "----riggle" + uuid.uuid4().hex
    head = b""
    for key, value in fields.items():
        head += (
            '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
            % (boundary, key, value)
        ).encode("utf-8")
    filename = os.path.basename(path).replace("\\", "_").replace('"', "%22")
    filename = filename.replace("\r", " ").replace("\n", " ")
    extension = os.path.splitext(path)[1].lower()
    head += (
        '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n"
        % (boundary, file_field, filename,
           _CONTENT_TYPES.get(extension, "application/octet-stream"))
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("ascii")

    def body():
        yield head
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        yield tail

    length = len(head) + os.path.getsize(path) + len(tail)
    return body(), length, "multipart/form-data; boundary=" + boundary


# -- command line ------------------------------------------------------------

class _Progress:
    """One line that says where the conversion is: rewritten in place on a
    terminal, printed once per stage in a log."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty()
        self.last = None
        self.open_line = False

    def __call__(self, conversion, elapsed):
        status = conversion.get("status") or "?"
        stage = conversion.get("stage")
        label = "%s: %s" % (status, stage) if stage else status
        if self.tty:
            self.stream.write("\r\033[K[%4ds] %s" % (elapsed, label))
            self.stream.flush()
            self.open_line = True
        elif label != self.last:
            print("[%4ds] %s" % (elapsed, label), file=self.stream, flush=True)
        self.last = label

    def done(self):
        if self.open_line:
            self.stream.write("\n")
            self.stream.flush()
            self.open_line = False


def _explain(exc):
    """An API error as something a person can act on."""
    lines = ["Error: %s" % exc.message]
    if exc.code and exc.code not in ("network", "timeout", "no_api_key"):
        status = " (HTTP %s)" % exc.status if exc.status else ""
        lines[0] = "Error: %s [%s]%s" % (exc.message, exc.code, status)
    if exc.code == "insufficient_tokens":
        required, available = exc.detail("required"), exc.detail("available")
        if required is not None and available is not None:
            lines.append("This needs %s API tokens; the account has %s."
                         % (required, available))
        lines.append("Buy API tokens at https://app.riggle3d.com/developers/")
    elif exc.code == "picture_rejected":
        for problem in exc.detail("problems") or []:
            lines.append("  - %s" % (problem.get("message") or problem.get("code")))
        lines.append("Nothing was charged. Send another picture, or this one "
                     "again with --fix (redraw it first, +2 API tokens) or "
                     "--force (use it as it is).")
    elif exc.code == "too_many_active":
        lines.append("Wait for a running job to finish, then try again.")
    elif exc.code == "unauthorized":
        lines.append("Check RIGGLE_API_KEY; keys are made and revoked at "
                     "https://app.riggle3d.com/developers/")
    return "\n".join(lines)


def _cmd_convert(client, args):
    if bool(args.file) == bool(args.image):
        print("Give a model file, or --image with a picture (not both).",
              file=sys.stderr)
        return 2
    if (args.fix or args.force) and not args.image:
        print("--fix and --force apply to --image only.", file=sys.stderr)
        return 2
    source = args.file or args.image
    output = args.output or os.path.splitext(source)[0] + ".vrm"
    progress = _Progress()
    print("Uploading %s" % source, file=sys.stderr, flush=True)
    try:
        conversion = client.convert(
            file=args.file, image=args.image, output=output, name=args.name,
            preview=args.preview, on_progress=progress, timeout=args.timeout,
            fix=args.fix, force=args.force)
    except ConversionFailed as exc:
        progress.done()
        print("Conversion %s failed: %s"
              % (exc.conversion.get("id"), exc.message), file=sys.stderr)
        if exc.conversion.get("refunded"):
            print("Its %s API tokens were returned."
                  % exc.conversion.get("cost"), file=sys.stderr)
        return 1
    finally:
        progress.done()
    for warning in conversion.get("warnings") or []:
        print("Warning: %s" % warning, file=sys.stderr)
    if conversion.get("warnings") and not args.preview:
        print("Look at the preview before shipping it: add --preview "
              "preview.mp4", file=sys.stderr)
    print("Saved %s (conversion %s, %s API tokens)"
          % (output, conversion.get("id"), conversion.get("cost")),
          file=sys.stderr)
    if args.preview:
        print("Saved %s" % args.preview, file=sys.stderr)
    return 0


def _cmd_fix_picture(client, args):
    output = args.output or os.path.splitext(args.image)[0] + ".fixed.png"
    progress = _Progress()
    print("Uploading %s" % args.image, file=sys.stderr, flush=True)
    try:
        fix = client.fix_picture(
            args.image, output, on_progress=progress, timeout=args.timeout)
    except PictureFixFailed as exc:
        progress.done()
        print("Picture fix %s failed: %s" % (exc.fix.get("id"), exc.message),
              file=sys.stderr)
        if exc.fix.get("refunded"):
            print("Its %s API tokens were returned." % exc.fix.get("cost"),
                  file=sys.stderr)
        return 1
    finally:
        progress.done()
    print("Saved %s (picture fix %s, %s API tokens)"
          % (output, fix.get("id"), fix.get("cost")), file=sys.stderr)
    return 0


def _cmd_balance(client, args):
    account = client.account()
    print("API tokens: %s" % account.get("api_tokens"))
    for action, cost in (account.get("prices") or {}).items():
        print("  %s: %s" % (action, cost))
    if account.get("max_active_conversions") is not None:
        print("Conversions in flight at once: %s"
              % account["max_active_conversions"])
    return 0


def _cmd_list(client, args):
    conversions = client.list(args.limit)
    if not conversions:
        print("No conversions yet.")
        return 0
    row = "%-36s  %-10s  %-16s  %-5s  %4s  %-20s  %s"
    print(row % ("ID", "STATUS", "STAGE", "INPUT", "COST", "CREATED", "NAME"))
    for c in conversions:
        status = c.get("status") or ""
        if status == "failed" and c.get("refunded"):
            status = "failed*"
        print(row % (c.get("id", ""), status, c.get("stage") or "-",
                     c.get("input") or "", c.get("cost", ""),
                     c.get("created_at") or "", c.get("name") or ""))
    if any(c.get("status") == "failed" and c.get("refunded")
           for c in conversions):
        print("* tokens returned")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="riggle",
        description="Turn a GLB/VRM model or a picture into a talking VRM 1.0 "
                    "avatar with the Riggle API.",
        epilog="Environment: RIGGLE_API_KEY (required), RIGGLE_API_URL "
               "(default %s). Reference: https://riggle3d.com/en/guides/vrm-api"
               % DEFAULT_API_URL,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser(
        "convert", help="upload a model or a picture, wait, download the VRM")
    convert.add_argument("file", nargs="?",
                         help="a .glb or .vrm, rigged or not (10 API tokens)")
    convert.add_argument("--image", metavar="PICTURE",
                         help="a .png or .jpg instead of a model (25 API tokens)")
    convert.add_argument("--fix", action="store_true",
                         help="with --image: if the picture check finds a problem, "
                              "redraw the picture first (+2 API tokens)")
    convert.add_argument("--force", action="store_true",
                         help="with --image: skip the picture check")
    convert.add_argument("-o", "--output", metavar="VRM",
                         help="where to save the VRM (default: input name + .vrm)")
    convert.add_argument("--preview", metavar="MP4",
                         help="also save the preview video of every expression")
    convert.add_argument("--name", help="what to call the conversion")
    convert.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                         metavar="SECONDS",
                         help="give up waiting after this long (default %d)"
                              % DEFAULT_TIMEOUT)
    convert.set_defaults(run=_cmd_convert)

    fix = commands.add_parser(
        "fix-picture",
        help="redraw a picture as one full-body front view (2 API tokens)")
    fix.add_argument("image", help="a .png or .jpg")
    fix.add_argument("-o", "--output", metavar="PNG",
                     help="where to save it (default: input name + .fixed.png)")
    fix.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                     metavar="SECONDS", help="give up waiting after this long")
    fix.set_defaults(run=_cmd_fix_picture)

    balance = commands.add_parser("balance", help="show the API token balance")
    balance.set_defaults(run=_cmd_balance)

    listing = commands.add_parser("list", help="show the latest conversions")
    listing.add_argument("--limit", type=int, default=20,
                         help="how many (default 20, max 100)")
    listing.set_defaults(run=_cmd_list)

    args = parser.parse_args(argv)
    try:
        return args.run(Riggle(), args)
    except RiggleError as exc:
        print(_explain(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. A conversion that was started keeps running; "
              "`list` shows it.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
