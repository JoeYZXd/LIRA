"""Keep conversational replies local and explicit dictation speech on the TV."""
import asyncio


class RealtimeAudioOutput:
    def __init__(self, device, television, is_explicit_speech):
        self.device = device
        self.television = television
        self.is_explicit_speech = is_explicit_speech

    @property
    def output_frames(self):
        return self.device.output_frames + self.television.output_frames

    async def open(self):
        # A sleeping or disconnected TV must not disable normal conversation.
        await self.device.open()

    def play_stream(self, chunks):
        # Capture the destination before scheduling; keep it for the whole stream.
        player = self.television if self.is_explicit_speech() else self.device
        return player.play_stream(chunks)

    async def stop(self):
        results = await asyncio.gather(self.device.stop(), self.television.stop(),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def close(self):
        results = await asyncio.gather(self.device.close(), self.television.close(),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
