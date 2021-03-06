from . import song
from ..annotation.util import *

logger = logging.getLogger('colorlogger')

circle_of_fifths = {
    'major': ['C', 'G', 'D', 'A', 'E', 'B', 'F#', 'C#', 'Ab', 'Eb', 'Bb', 'F'],
    'minor': ['A', 'E', 'B', 'F#', 'C#', 'Ab', 'Eb', 'Bb', 'F', 'C', 'G', 'D']
}
notes = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']


def get_key(key, scale, offset, switchMajorMinor=False):
    idx = (circle_of_fifths[scale].index(key) + offset) % 12
    scale2 = scale if not switchMajorMinor else ('major' if scale == 'minor' else 'minor')
    return circle_of_fifths[scale2][idx], scale2


def get_key_transposed(key, scale, semitones):
    idx = notes.index(key)
    return notes[(idx + semitones) % 12], scale


def get_relative_key(key, scale):
    if scale == 'major':
        new_key, _ = get_key_transposed(key, scale, -3)
        return new_key, 'minor'
    else:
        new_key, _ = get_key_transposed(key, scale, 3)
        return new_key, 'major'


def get_closely_related_keys(key, scale):
    result = []
    result.append((key, scale))
    result.append(get_relative_key(key, scale))
    result.append(get_key_transposed(key, scale, 7))
    result.append(get_key_transposed(key, scale, -7))
    return result


def distance_keys_semitones(key1, key2):
    idx1 = notes.index(key1)
    idx2 = notes.index(key2)
    return (idx2 - idx1) % 12


def distance_keys_circle_of_fifths(key1, scale1, key2, scale2):
    idx1 = circle_of_fifths[scale1].index(key1)
    idx2 = circle_of_fifths[scale2].index(key2)
    return ((6 + ((idx2 - idx1) % 12)) % 12) - 6


class SongCollection:
    def __init__(self, annotation_modules):
        self.songs = []
        self.directories = []
        self.key_title = {}
        self.annotation_modules = annotation_modules

    def init_key_title_map(self):
        self.key_title = {}
        annotated_titles = [s.title for s in self.get_annotated()]
        for s in self.songs:
            if not s.title in annotated_titles:
                continue
            if not s.key in self.key_title:
                self.key_title[s.key] = [s.title]
            else:
                self.key_title[s.key].append(s.title)
        for key, songs in iter(sorted(self.key_title.items())):
            logger.info('Key {} :\t{} songs'.format(key, len(songs)))
        if len(self.key_title) == 0:
            logger.warning("Key-title map is empty!")

    def clear(self):
        self.songs = []
        self.directories = []
        self.key_title = []

    def load_directory(self, directory):
        directory_ = os.path.abspath(directory)
        if directory_ in self.directories:
            return
        logger.info('Loading directory ' + directory + '...')
        self.directories.append(directory_)
        self.songs.extend([
            song.Song(os.path.join(directory_, f), annotation_modules=self.annotation_modules)
            for f in os.listdir(directory_)
            if os.path.isfile(os.path.join(directory_, f)) and (f.endswith('.wav') or f.endswith('.mp3'))
        ])
        for s in self.songs:
            s.open()
        self.init_key_title_map()

    def annotate(self):
        for s in self.get_unannotated():
            s.annotate()
        self.init_key_title_map()

    def get_unannotated(self):
        return [s for s in self.songs if not s.hasAllAnnot()]

    def get_annotated(self):
        return [s for s in self.songs if s.hasAllAnnot()]

    def get_marked(self):
        markedTitles = []
        with open('markfile.csv') as csvfile:
            reader = csv.reader(csvfile)
            for line in reader:
                print(line)
                markedTitles.extend(line)
        return [s for s in self.songs if s.title in markedTitles]

    def get_titles_in_key(self, key, scale, offset=0, switchMajorMinor=False):
        result = []
        key, scale = get_key(key, scale, offset, switchMajorMinor)
        try:
            titles_to_add = self.key_title[key + ':' + scale]
            result += titles_to_add
        except KeyError:
            pass
        return result


if __name__ == '__main__':
    from .controller import DjController
    from .tracklister import TrackLister

    sc = SongCollection()
    sc.load_directory('../music/')
    sc.songs[0].open()
    logger.debug(sc.songs[0].tempo)
    tl = TrackLister(sc)
    tl.generate(10)
    sm = DjController(tl)
    sm.play()
