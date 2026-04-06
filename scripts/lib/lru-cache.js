/**
 * Simple LRU (Least Recently Used) cache.
 *
 * Uses Map insertion order to track recency — most recent entries
 * are at the end. On access (get) or update (set), the entry is
 * moved to the end. When size exceeds maxSize, the oldest (first)
 * entry is evicted.
 */
export class LRUCache {
  /**
   * @param {number} maxSize — maximum number of entries before eviction
   */
  constructor(maxSize) {
    this._max = maxSize;
    this._map = new Map();
  }

  /**
   * Retrieve a value and promote it to most-recent.
   * @param {*} key
   * @returns {*} value or undefined
   */
  get(key) {
    if (!this._map.has(key)) return undefined;
    const val = this._map.get(key);
    // Move to end (most recent)
    this._map.delete(key);
    this._map.set(key, val);
    return val;
  }

  /**
   * Store a value, promoting it to most-recent.
   * Evicts the oldest entry if size exceeds max.
   * @param {*} key
   * @param {*} value
   */
  set(key, value) {
    this._map.delete(key);
    this._map.set(key, value);
    if (this._map.size > this._max) {
      const oldest = this._map.keys().next().value;
      this._map.delete(oldest);
    }
  }

  /** @param {*} key */
  has(key) {
    return this._map.has(key);
  }

  /** @param {*} key */
  delete(key) {
    return this._map.delete(key);
  }

  get size() {
    return this._map.size;
  }

  clear() {
    this._map.clear();
  }

  values() {
    return this._map.values();
  }

  entries() {
    return this._map.entries();
  }

  [Symbol.iterator]() {
    return this._map[Symbol.iterator]();
  }
}
