"""Limit request bytes for the workspace upload route before multipart parsing."""
from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse


class UploadRequestLimit:
    # 20 maximum-size files plus 1 MiB for multipart framing/fields.
    max_bytes = 401 * 1024 * 1024

    def __init__(self, app, max_bytes=None):
        self.app = app
        if max_bytes is not None:
            self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'] != '/api/workspace/upload':
            await self.app(scope, receive, send)
            return
        # A declared length can reject early, but is never trusted as the only
        # check: chunked requests and incorrect lengths are counted as received.
        for name, value in scope.get('headers', []):
            if name.lower() == b'content-length':
                try:
                    length = int(value)
                except ValueError:
                    continue
                if length > self.max_bytes:
                    await JSONResponse({'detail': 'Upload request too large'}, status_code=413)(scope, receive, send)
                    return
        received = 0
        exceeded = False
        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > self.max_bytes:
                    exceeded = True
                    # Older Starlette only closes multipart spool files for this
                    # exception type. Request.form converts it to an HTTP 400.
                    raise MultiPartException('Upload request too large')
            return message
        async def limited_send(message):
            if exceeded and message['type'] == 'http.response.start':
                message = {**message, 'status': 413}
            await send(message)
        await self.app(scope, limited_receive, limited_send)
