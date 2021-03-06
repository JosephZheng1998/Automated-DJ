import bisect
import csv
import ctypes
import logging
import multiprocessing
from multiprocessing import Process, Queue
from time import sleep

import numpy as np
import pyaudio
from essentia.standard import MonoWriter, AudioWriter

from . import songtransitions
from . import tracklister
from .timestretching import time_stretch_and_pitch_shift

logger = logging.getLogger('colorlogger')


class DjController:
    def __init__(self, tracklister, stereo=True):
        self.tracklister = tracklister
        self.stereo = stereo
        self.audio_thread = None
        self.dj_thread = None
        self.playEvent = multiprocessing.Event()
        self.isPlaying = multiprocessing.Value('b', True)
        self.skipFlag = multiprocessing.Value('b', False)
        self.queue = Queue(6)
        self.currentMasterString = multiprocessing.Manager().Value(ctypes.c_char_p, '')
        self.pyaudio = None
        self.stream = None
        self.djloop_calculates_crossfade = False
        self.save_mix = False
        self.save_dir_idx = 0
        self.save_dir = './mix_{}.wav'
        self.save_dir_tracklist = './mix.txt'
        self.audio_to_save = None
        self.audio_save_queue = Queue(6)
        self.save_tracklist = []

    def play(self, save_mix=False):
        self.playEvent.set()
        if self.dj_thread is None and self.audio_thread is None:
            self.save_mix = save_mix
            self.save_dir_idx = 0
            self.audio_to_save = []
            self.save_tracklist = []
            if self.save_mix:
                Process(target=self._flush_save_audio_buffer, args=(self.audio_save_queue,)).start()
            self.dj_thread = Process(target=self._dj_loop, args=(self.isPlaying,))
            self.audio_thread = Process(target=self._audio_play_loop,
                                        args=(self.playEvent, self.isPlaying, self.currentMasterString))
            self.isPlaying.value = True
            self.dj_thread.start()
            while self.queue.empty():
                sleep(0.1)
            self.audio_thread.start()
        elif self.dj_thread is None or self.audio_thread is None:
            raise Exception('dj_thread and audio_thread are not both Null!')

    def save_audio_to_disk(self, audio, song_title):
        self.audio_to_save.append(audio)
        self.save_tracklist.append(song_title)
        if np.sum([len(a) for a in self.audio_to_save]) > 44100 * 60 * 15:
            self.flush_audio_to_queue()

    def flush_audio_to_queue(self):
        self.save_dir_idx += 1
        self.audio_to_save = np.concatenate(self.audio_to_save, axis=-1)
        self.audio_save_queue.put((
            self.save_dir.format(self.save_dir_idx), np.array(self.audio_to_save, dtype='single'),
            self.save_tracklist))
        self.audio_to_save = []
        self.save_tracklist = []

    def _flush_save_audio_buffer(self, queue):
        while True:
            filename, audio, tracklist = queue.get()
            if not (filename is None):
                logger.debug('Saving {} to disk, length {}'.format(filename, len(audio)))
                if self.stereo:
                    writer = AudioWriter(filename=filename, format='wav')
                else:
                    writer = MonoWriter(filename=filename, format='wav')
                writer(np.array(audio.T, dtype='single'))
                logger.debug('Saving tracklist')
                with open(self.save_dir_tracklist, 'a+') as csvfile:
                    writer = csv.writer(csvfile)
                    for line in tracklist:
                        writer.writerow([line])
            else:
                logger.debug('Stopping audio saving thread!')
                return

    def skipToNextSegment(self):
        if not self.queue.empty():
            self.skipFlag.value = True
        else:
            self.skipFlag.value = False
            logger.warning('Cannot skip to next segment, no audio in queue!')

    def markCurrentMaster(self):
        with open('markfile.csv', 'a+') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([self.currentMasterString.value])
        logger.debug('{:20s} has been marked for manual annotation.'.format(self.currentMasterString.value))

    def pause(self):
        if self.audio_thread is None:
            return
        self.playEvent.clear()

    def stop(self):
        try:
            self.playEvent.set()
        except Exception as e:
            logger.debug(e)
        self.isPlaying.value = False
        while not self.queue.empty():
            self.queue.get_nowait()
        if not self.dj_thread is None:
            self.dj_thread.terminate()
        self.queue = Queue(6)
        self.audio_thread = None
        self.dj_thread = None
        if not self.stream is None:
            self.stream.stop_stream()
            self.stream.close()
        if not self.pyaudio is None:
            self.pyaudio.terminate()
        self.pyaudio = None

    def _audio_play_loop(self, playEvent, isPlaying, currentMasterString):
        if self.pyaudio is None:
            self.pyaudio = pyaudio.PyAudio()
        if self.stream is None:
            self.stream = self.pyaudio.open(format=pyaudio.paFloat32, channels=1 if not self.stereo else 2, rate=44100,
                                            output=True)
        while isPlaying.value:
            toPlay, toPlayStr, masterTitle = self.queue.get()
            logger.info(toPlayStr)
            currentMasterString.value = masterTitle
            if toPlay is None:
                break
            else:
                if self.save_mix:
                    self.save_audio_to_disk(toPlay, masterTitle)
            FRAME_LEN = 1024
            last_frame_start_idx = int(toPlay.shape[-1] / FRAME_LEN) * FRAME_LEN
            for cur_idx in range(0, last_frame_start_idx + 1, FRAME_LEN):
                playEvent.wait()
                if not self.isPlaying.value:
                    break
                if self.skipFlag.value:
                    self.skipFlag.value = False
                    break
                if cur_idx == last_frame_start_idx:
                    end_idx = toPlay.shape[-1]
                else:
                    end_idx = cur_idx + FRAME_LEN
                toPlayNow = toPlay[..., cur_idx:end_idx]
                if toPlayNow.dtype != 'float32':
                    toPlayNow = toPlayNow.astype('float32')
                toPlayNow = np.copy(toPlayNow.T, order='C')
                self.stream.write(toPlayNow, num_frames=toPlayNow.shape[0], exception_on_underflow=False)
        logger.debug('Stopping music')
        if self.save_mix:
            logger.debug('Flushing audio to disk...')
            self.flush_audio_to_queue()
            self.audio_save_queue.put((None, None, None))

    def _dj_loop(self, isPlaying):
        TEMPO = 175
        samples_per_dbeat = 44100 * 4 * 60 / TEMPO
        song_titles_in_buffer = []
        tracklist_changes = []
        num_songs_playing = 0
        songs_playing_master = 0

        def add_song_to_tracklist(master_song, anchor_sample, next_song, next_fade_type, cue_master_out, fade_in_len,
                                  fade_out_len):
            f = master_song.tempo / TEMPO
            buffer_in_sample = int(f * (44100 * master_song.downbeats[cue_master_out] - anchor_sample))
            buffer_switch_sample = int(
                f * (44100 * master_song.downbeats[cue_master_out] - anchor_sample) + fade_in_len * samples_per_dbeat)
            buffer_out_sample = int(f * (44100 * master_song.downbeats[cue_master_out] - anchor_sample) + (
                    fade_in_len + fade_out_len) * samples_per_dbeat)
            song_titles_in_buffer.append(next_song.title)
            bisect.insort(tracklist_changes, (buffer_in_sample, 'in', next_fade_type))
            bisect.insort(tracklist_changes, (buffer_switch_sample, 'switch', next_fade_type))
            bisect.insort(tracklist_changes, (buffer_out_sample, 'out', next_fade_type))

        def curPlayingString(fade_type_str):
            outstr = 'Now playing:\n'
            for i in range(num_songs_playing):
                if i != songs_playing_master:
                    outstr += song_titles_in_buffer[i] + '\n'
                else:
                    outstr += song_titles_in_buffer[i].upper() + '\n'
            if fade_type_str != '':
                outstr += '[' + fade_type_str + ']'
            return outstr

        if self.save_mix:
            self.audio_to_save = []
            self.save_tracklist = []

        current_song = self.tracklister.getFirstSong()
        current_song.open()
        current_song.openAudio()
        anchor_sample = 0
        cue_master_in = current_song.segment_indices[0]
        fade_in_len = 16
        prev_fade_type = tracklister.TYPE_CHILL
        logger.debug('FIRST SONG: {}'.format(current_song.title))

        cue_master_out, next_fade_type, max_fade_in_len, fade_out_len = tracklister.getMasterQueue(
            current_song, cue_master_in + fade_in_len, prev_fade_type)
        next_song, cue_next_in, cue_master_out, fade_in_len, semitone_offset = \
            self.tracklister.getBestNextSongAndCrossfade(
                current_song, cue_master_out, max_fade_in_len, fade_out_len, next_fade_type)
        song_titles_in_buffer.append(current_song.title)
        add_song_to_tracklist(current_song, anchor_sample, next_song, next_fade_type, cue_master_out, fade_in_len,
                              fade_out_len)
        prev_in_or_out = 'in'

        f = current_song.tempo / TEMPO
        print(f'CURRENT SONG TEMPO, STRETCH: {current_song.tempo} {f}')
        current_audio_start = 0
        current_audio_end = int(
            (current_song.downbeats[cue_master_out] * 44100) + (fade_in_len + fade_out_len + 2) * samples_per_dbeat / f)
        if self.stereo:
            current_audio_stretched = np.array((
                time_stretch_and_pitch_shift(
                    np.asfortranarray(current_song.audio_left[current_audio_start:current_audio_end]), f),
                time_stretch_and_pitch_shift(
                    np.asfortranarray(current_song.audio_right[current_audio_start:current_audio_end]), f)
            ))
        else:
            current_audio_stretched = time_stretch_and_pitch_shift(
                current_song.audio[current_audio_start:current_audio_end], f)

        mix_buffer = current_audio_stretched
        mix_buffer_cf_start_sample = int(f * (current_song.downbeats[cue_master_out] * 44100))

        while True:
            prev_end_sample = 0
            for end_sample, in_or_out, cur_fade_type in tracklist_changes:

                if end_sample > mix_buffer_cf_start_sample:
                    break

                if prev_in_or_out == 'in':
                    num_songs_playing += 1
                elif prev_in_or_out == 'out':
                    num_songs_playing -= 1
                    songs_playing_master -= 1
                    song_titles_in_buffer = song_titles_in_buffer[1:]
                elif prev_in_or_out == 'switch':
                    songs_playing_master += 1
                prev_in_or_out = in_or_out

                if end_sample > prev_end_sample:
                    toPlay = mix_buffer[..., prev_end_sample: end_sample]
                    cur_fade_type_str = cur_fade_type if num_songs_playing > 1 else ''
                    toPlayTuple = (
                        toPlay, curPlayingString(cur_fade_type_str), song_titles_in_buffer[songs_playing_master])
                    self.queue.put(toPlayTuple, isPlaying.value)
                    prev_end_sample = end_sample

            tracklist_changes = [(tc[0] - mix_buffer_cf_start_sample, tc[1], tc[2])
                                 for tc in tracklist_changes if tc[0] > mix_buffer_cf_start_sample]
            mix_buffer = mix_buffer[..., mix_buffer_cf_start_sample:]
            current_song.close()

            current_song = next_song
            current_song.open()
            f = current_song.tempo / TEMPO
            cue_master_in = cue_next_in
            prev_fade_type = next_fade_type
            prev_fade_in_len = fade_in_len
            prev_fade_out_len = fade_out_len

            cue_master_out, next_fade_type, max_fade_in_len, fade_out_len = \
                tracklister.getMasterQueue(current_song, cue_master_in + fade_in_len, prev_fade_type)
            next_song, cue_next_in, cue_master_out, fade_in_len, semitone_offset = \
                self.tracklister.getBestNextSongAndCrossfade(
                    current_song, cue_master_out, max_fade_in_len, fade_out_len, next_fade_type)
            anchor_sample = int(44100 * current_song.downbeats[cue_master_in])
            add_song_to_tracklist(current_song, anchor_sample, next_song, next_fade_type, cue_master_out, fade_in_len,
                                  fade_out_len)
            mix_buffer_cf_start_sample = int(f * (current_song.downbeats[cue_master_out] * 44100 - anchor_sample))

            f = current_song.tempo / TEMPO
            current_song.openAudio()

            current_audio_start = int(current_song.downbeats[cue_master_in] * 44100)
            current_audio_end = int((current_song.downbeats[cue_master_out] * 44100) + (
                    fade_in_len + fade_out_len + 2) * samples_per_dbeat / f)

            if self.stereo:
                current_audio_stretched = np.array((
                    time_stretch_and_pitch_shift(
                        np.asfortranarray(current_song.audio_left[current_audio_start:current_audio_end]), f,
                        semitones=semitone_offset),
                    time_stretch_and_pitch_shift(
                        np.asfortranarray(current_song.audio_right[current_audio_start:current_audio_end]), f,
                        semitones=semitone_offset)
                ))
            else:
                current_audio_stretched = time_stretch_and_pitch_shift(
                    current_song.audio[current_audio_start:current_audio_end], f, semitones=semitone_offset)

            cf = songtransitions.CrossFade(0, [0], prev_fade_in_len + prev_fade_out_len, prev_fade_in_len,
                                           prev_fade_type)
            if self.stereo:
                mix_buffer_deepcpy = np.array(mix_buffer, dtype='single', copy=True)
                mix_buffer_left = cf.apply(mix_buffer_deepcpy[0], current_audio_stretched[0], TEMPO)
                mix_buffer_right = cf.apply(mix_buffer_deepcpy[1], current_audio_stretched[1], TEMPO)
                mix_buffer = np.array((mix_buffer_left, mix_buffer_right), dtype='single', copy=True)
            else:
                mix_buffer_deepcpy = np.array(mix_buffer, dtype='single', copy=True)
                mix_buffer = cf.apply(mix_buffer_deepcpy, current_audio_stretched, TEMPO)
