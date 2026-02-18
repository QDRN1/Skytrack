"""SkyTrack offline reverse geocoder — nearest-neighbor against US city/ZIP CSVs.

No heavy GIS libraries required.  Loads ``us_cities.csv`` and
``us_zip_centroids.csv`` into memory and uses haversine distance to find the
nearest city and ZIP centroid for a given lat/lon.

Result is formatted as ``City, ST ZIP`` and cached to disk.  Recomputation
only happens when the device has moved >1 mile or the cache is >6 hours old.
"""

import csv
import json
import logging
import math
import os
import time

logger = logging.getLogger('skytrack.geocode')

# Earth radius in miles
_R_MILES = 3958.8

# Recompute thresholds
_MOVE_THRESHOLD_MILES = 1.0
_AGE_THRESHOLD_SECS = 6 * 3600  # 6 hours


def _haversine(lat1, lon1, lat2, lon2):
    """Return distance in miles between two lat/lon points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return _R_MILES * 2 * math.asin(math.sqrt(a))


def _load_csv(path, lat_col, lon_col):
    """Load a CSV and return list of dicts with float lat/lon."""
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    row['_lat'] = float(row[lat_col])
                    row['_lon'] = float(row[lon_col])
                    rows.append(row)
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        logger.warning('Geodata file not found: %s', path)
    except Exception as exc:
        logger.warning('Error loading %s: %s', path, exc)
    return rows


class OfflineGeocoder:
    """Reverse geocoder using local CSV datasets."""

    def __init__(self, config):
        self._config = config
        self._enabled = config.get('geo_offline_enabled', True)
        self._data_dir = config.get('geo_data_dir', '/var/lib/skytrack/geo')
        self._cache_path = os.path.join(self._data_dir, 'last_location.json')

        self._cities = []
        self._zips = []
        self._loaded = False
        self._cached_result = None
        self._cached_lat = None
        self._cached_lon = None
        self._cached_time = 0

        if self._enabled:
            self._load_datasets()

    def _load_datasets(self):
        """Load city and ZIP CSV files into memory."""
        cities_path = os.path.join(self._data_dir, 'us_cities.csv')
        zips_path = os.path.join(self._data_dir, 'us_zip_centroids.csv')

        self._cities = _load_csv(cities_path, 'lat', 'lon')
        self._zips = _load_csv(zips_path, 'lat', 'lon')

        if self._cities or self._zips:
            self._loaded = True
            logger.info('Geodata loaded: %d cities, %d ZIP codes', len(self._cities), len(self._zips))
        else:
            logger.warning('No geodata files found in %s — location labels disabled', self._data_dir)

        # Try to load disk cache
        self._load_cache()

    def _load_cache(self):
        """Load cached location from disk."""
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path, 'r') as f:
                    data = json.load(f)
                self._cached_result = data.get('label')
                self._cached_lat = data.get('lat')
                self._cached_lon = data.get('lon')
                self._cached_time = data.get('timestamp', 0)
        except Exception as exc:
            logger.debug('Cache load error: %s', exc)

    def _save_cache(self, label, lat, lon):
        """Persist resolved location to disk."""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._cache_path, 'w') as f:
                json.dump({
                    'label': label,
                    'lat': lat,
                    'lon': lon,
                    'timestamp': time.time(),
                }, f)
        except Exception as exc:
            logger.debug('Cache save error: %s', exc)

    def _needs_recompute(self, lat, lon):
        """Check if we need to re-resolve the location."""
        if self._cached_result is None:
            return True
        age = time.time() - self._cached_time
        if age > _AGE_THRESHOLD_SECS:
            return True
        if self._cached_lat is not None and self._cached_lon is not None:
            dist = _haversine(lat, lon, self._cached_lat, self._cached_lon)
            if dist > _MOVE_THRESHOLD_MILES:
                return True
        return False

    def _find_nearest(self, lat, lon, dataset):
        """Return the nearest row from a dataset."""
        best = None
        best_dist = float('inf')
        for row in dataset:
            d = _haversine(lat, lon, row['_lat'], row['_lon'])
            if d < best_dist:
                best_dist = d
                best = row
        return best, best_dist

    def resolve(self, lat, lon):
        """Resolve lat/lon to a ``City, ST ZIP`` label.

        Returns a dict with ``label``, ``city``, ``state``, ``zip``, ``distance_mi``.
        Returns None if geocoding is disabled or datasets are missing.
        """
        if not self._enabled or not self._loaded:
            # Return cached result if available
            if self._cached_result:
                return {'label': self._cached_result, 'cached': True}
            return None

        if not self._needs_recompute(lat, lon):
            return {
                'label': self._cached_result,
                'city': None,
                'state': None,
                'zip': None,
                'cached': True,
            }

        city_name = ''
        state_abbr = ''
        zip_code = ''

        if self._cities:
            nearest_city, _ = self._find_nearest(lat, lon, self._cities)
            if nearest_city:
                city_name = nearest_city.get('city', '')
                state_abbr = nearest_city.get('state', '')

        if self._zips:
            nearest_zip, _ = self._find_nearest(lat, lon, self._zips)
            if nearest_zip:
                zip_code = nearest_zip.get('zip', '')
                # Use ZIP's state if city dataset didn't provide one
                if not state_abbr:
                    state_abbr = nearest_zip.get('state', '')

        if city_name and state_abbr and zip_code:
            label = f'{city_name}, {state_abbr} {zip_code}'
        elif city_name and state_abbr:
            label = f'{city_name}, {state_abbr}'
        elif city_name:
            label = city_name
        else:
            label = 'Location unavailable'

        self._cached_result = label
        self._cached_lat = lat
        self._cached_lon = lon
        self._cached_time = time.time()
        self._save_cache(label, lat, lon)

        return {
            'label': label,
            'city': city_name,
            'state': state_abbr,
            'zip': zip_code,
            'cached': False,
        }

    def get_label(self, lat, lon):
        """Convenience: return just the formatted label string."""
        result = self.resolve(lat, lon)
        if result:
            return result.get('label', 'Location unavailable')
        return None
