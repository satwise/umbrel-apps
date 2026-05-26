import { relayInit } from "nostr-tools";
import "websocket-polyfill"; // polyfill for relayInit in nodejs
import pRetry from "p-retry";

import {
  readIdentifierFromFile,
  writeIdentifierToFile,
  deleteStoreFile,
  getPublicKeyAndRelaysFromIdentifier,
} from "./helpers.js";
import { LOCAL_RELAY_URL, DISCOVERY_STATUS } from "./constants.js";

/*
Relay discovery and connection logic is as follows:
1. Await connection to local nostr-rs-relay
2. Connect to relays from NIP-05, or from a list of popular relays if only an npub is provided
  - retry connection attempts indefinitely, with exponential backoff to max timeout of 1 hour
3. Subscribe to events
  - Publish events to local nostr-rs-relay
  - If a Kind 3 Contact List event is received, connect to any new relays in that list that we are not already connected to and subscribe to events from those relays
  - Periodically close any relays that are not in the most recent Kind 3 Contact List Event
*/

class RelayManager {
  constructor() {
    this.resetState();
    this.establishRelayConnections();
  }

  resetState() {
    this.relays = {};
    this.savedRelayOverrides = [];
    // latestRelays tracks relays from the most recent Kind 3 Contact List Event (or default relays if no Kind 3 Contact List Events have been received)
    this.latestRelays = null; // { relays, timestamp }
    this.identifier = null; // store to avoid having to read from file for getConnectionStatus
    this.pubkey = null; // hex formatted pubkey
    this.status = DISCOVERY_STATUS.IDLE;
    this.firstEventReceived = false;
    if (this.cleanUpRelaysInterval) {
      clearInterval(this.cleanUpRelaysInterval);
      this.cleanUpRelaysInterval = null;
    }
    if (this.ensureRelayConnectionsInterval) {
      clearInterval(this.ensureRelayConnectionsInterval);
      this.ensureRelayConnectionsInterval = null;
    }
  }

  // ===============================
  // Connection Methods
  // ===============================

  async establishRelayConnections() {
    // We await successful connection to local nostr-rs-relay or else events from public relays will not be published to the local relay
    try {
      this.localRelay = await this.initializeRelay(LOCAL_RELAY_URL, {
        isPrivate: true,
      });
    } catch (error) {
      console.log(error?.message); // thrown by initializeRelay after all retry attempts have failed
      return;
    }
    // Connect to public relays discovered from NIP-05 or npub
    this.discoverAndConnectToRelays();
  }

  async initializeRelay(url, options = {}) {
    const isPrivate = options.isPrivate || false;

    const relay = relayInit(url);

    // we only add a relay to the this.relays object if it is not the user's local nostr-rs-relay
    // this is done before we return the connectionPromise so that we can check if the relay is already being initialized when new Kind 3 Contact List events are received, even if the relay is not yet connected
    if (!isPrivate) {
      this.relays[url] = relay;
    }

    relay.on("connect", () => console.log(`Connected to ${relay.url}`));

    // connectRelay returns a promise that resolves when the relay connects or rejects when the relay fails to connect
    // so that we can use p-retry to retry connection attempts with exponential backoff.
    const connectRelay = () => {
      // error objects do not seem to ever be passed to relay.connect or the on error event handler
      return new Promise((resolve, reject) => {
        relay
          .connect()
          .then(resolve)
          .catch((error) => {
            reject(
              error ||
                new Error(
                  `Connection to ${relay.url} failed without an error object.`,
                ),
            );
          });

        relay.on("error", (error) => {
          reject(
            error ||
              new Error(
                `Error event from ${relay.url} without an error object.`,
              ),
          );
        });
      });
    };

    // abortController is used to abort the p-retry connection attempts if the relay is removed from this.relays object while the connection attempt is in progress
    // this would occur if cleanup of old relays occured while a connection attempt was in progress
    const abortController = new AbortController();

    try {
      await pRetry(connectRelay, {
        forever: true, // retry forever or until abortController.abort is called
        maxTimeout: 1000 * 60 * 60, // max timeout between retries is 1 hour
        signal: abortController.signal,
        onFailedAttempt: (error) => {
          console.log(
            `Attempt ${error.attemptNumber} to connect to ${relay.url} failed.`,
          );

          // we do not abort for our local relay
          if (!isPrivate && !this.relays[url]) {
            abortController.abort(
              `Additional connection attempts to ${relay.url} aborted because relay is no longer in user's Kind 3 Contact List.`,
            );
          }
        },
      });
    } catch (error) {
      console.log(error.message);
      throw new Error(
        `All attempts to connect to ${relay.url} have failed. No further attempts will be made.`,
      );
    }

    return relay;
  }

  async discoverAndConnectToRelays() {
    if (this.cleanUpRelaysInterval) {
      clearInterval(this.cleanUpRelaysInterval);
    }

    const identifierData = await readIdentifierFromFile();
    // we return early if there is no identifier set
    if (!identifierData) return;

    this.identifier = identifierData.identifier;
    this.savedRelayOverrides = this.dedupeRelayUrls(identifierData.relays);
    console.log(`NIP-05/NIP-19 Identifier: ${this.identifier}`);

    this.status = DISCOVERY_STATUS.DISCOVERING_RELAYS;

    const { pubkey, relays } = await getPublicKeyAndRelaysFromIdentifier(
      this.identifier,
    );
    const effectiveRelays = this.dedupeRelayUrls([
      ...(relays || []),
      ...this.savedRelayOverrides,
    ]);
    this.pubkey = pubkey;
    this.latestRelays = { relays: effectiveRelays, timestamp: 0 };

    for (const url of effectiveRelays) {
      this.connectAndSubscribeToRelay(url);
    }

    // Clean up relays that are not in the most recent Kind 3 Contact List Event every hour
    this.cleanUpRelaysInterval = setInterval(
      () => {
        this.removeOutdatedRelays();
      },
      60 * 60 * 1000,
    );

    // Reconnect relays that dropped unexpectedly.
    this.ensureRelayConnectionsInterval = setInterval(() => {
      this.ensureRelayConnections();
    }, 60 * 1000);
  }

  getExpectedRelayUrls() {
    let latestRelays = [];
    try {
      if (
        this.latestRelays &&
        typeof this.latestRelays.relays === "string" &&
        this.latestRelays.relays
      ) {
        latestRelays = Object.keys(JSON.parse(this.latestRelays.relays));
      } else if (Array.isArray(this.latestRelays?.relays)) {
        latestRelays = this.latestRelays.relays;
      }
    } catch (e) {
      /* ignore parse errors */
    }

    return this.dedupeRelayUrls([...latestRelays, ...this.savedRelayOverrides]);
  }

  ensureRelayConnections() {
    const expectedRelays = this.getExpectedRelayUrls();

    for (const url of expectedRelays) {
      const relay = this.relays[url];

      if (!relay) {
        this.connectAndSubscribeToRelay(url);
        continue;
      }

      // status 3 = CLOSED. Remove stale socket and re-initialize.
      if (relay.status === 3) {
        this.closeRelay(url);
        this.connectAndSubscribeToRelay(url);
      }
    }
  }

  async connectAndSubscribeToRelay(url) {
    // We ignore all unencrypted relays (beginning with 'ws://') as a way to filter out
    // a user's local nostr-rs-relay, which could be added to their clients or NIP-05.
    // This is a provisional measure, given the vast array of URLs that might be used,
    // including Tailscale magicDNS, local network domain name, IP address, etc.
    // This approach caters to the majority of use cases.
    if (url.startsWith("ws://")) return;

    // we return early if the relay is currently being, or already is, initialized
    if (this.relays[url]) return;

    try {
      const relay = await this.initializeRelay(url);
      if (relay) {
        // Follow the relay firehose (all kinds/authors) and keep a short warm window
        // so the dashboard catches up quickly after reconnects.
        const sub = relay.sub([{ since: Math.floor(Date.now() / 1000) - 900 }]);
        sub.on("event", (event) => this.handleEvent(event, relay));
      }
    } catch (error) {
      console.error(error?.message);
    }
  }

  // ===============================
  // Event Handling Methods
  // ===============================

  handleEvent(event, relay) {
    if (this.status === DISCOVERY_STATUS.DISCOVERING_RELAYS) {
      this.status = DISCOVERY_STATUS.IDLE;
    }

    // we set firstEventReceived to true after receiving the first event from a public relay so that we can show the SyncConfirmationModal ASAP in the UI
    if (!this.firstEventReceived) {
      this.firstEventReceived = true;
    }

    // If a Kind 3 Contact List event is received with a newer created_at timestamp, we connect to any new relays in that list
    // that we are not already connected to and subscribe to events from those relays
    if (event.kind === 3 && event.pubkey === this.pubkey) {
      if (
        this.latestRelays === null ||
        event.created_at > this.latestRelays.timestamp
      ) {
        console.log(
          `A more recent Kind 3 event was received from ${relay.url} with date: ${event.created_at}`,
        );
        this.latestRelays = {
          relays: event.content,
          timestamp: event.created_at,
        };
        const newRelays = Object.keys(JSON.parse(event.content));
        for (const url of newRelays) {
          this.connectAndSubscribeToRelay(url);
        }
      }
    }

    try {
      const publication = this.localRelay.publish(event);
      if (publication && typeof publication.on === "function") {
        publication.on("failed", (reason) => {
          console.error(
            `Local relay publish failed for event ${event.id}: ${reason}`,
          );
        });
      }
    } catch (error) {
      console.error("Error publishing to local relay:", error);
    }
  }

  // =====================
  // Relay Management Methods
  // =====================

  removeOutdatedRelays() {
    const latestRelays = this.getExpectedRelayUrls();
    const relaysToRemove = Object.keys(this.relays).filter(
      (url) => !latestRelays.includes(url),
    );
    if (relaysToRemove.length > 0) {
      console.log(`Removing outdated relays: ${relaysToRemove}`);
    }

    for (const url of relaysToRemove) {
      this.closeRelay(url);
    }
  }

  closeRelay(url) {
    const relay = this.relays[url];
    try {
      relay.close();
      delete this.relays[url];
      console.log(`${url} connection closed`);
    } catch (error) {
      console.log(`Error closing ${url}`);
    }
  }

  getConnectionStatus() {
    /* 
    'web socket readyState values are:
      - 0: CONNECTING
      - 1: OPEN
      - 2: CLOSING
      - 3: CLOSED
    */

    // we map relayStates to an array of objects with url and readyState properties, while filtering out relays that may cause errors.
    const relayStates = Object.entries(this.relays)
      .map(([key, relay]) => {
        if (relay.url && relay.status) {
          return {
            url: relay.url,
            readyState: relay.status, // this is a getter
          };
        } else {
          return null;
        }
      })
      .filter((state) => state !== null);

    return {
      identifier: this.identifier,
      status: this.status,
      firstEventReceived: this.firstEventReceived,
      relayStates,
    };
  }

  // ==========================
  // NIP-05/NIP-19 Identifier Management Methods
  // ==========================

  async addIdentifier(identifier) {
    await writeIdentifierToFile(identifier);
    this.discoverAndConnectToRelays();
  }

  async removeIdentifier() {
    await deleteStoreFile();
    for (const url of Object.keys(this.relays)) {
      this.closeRelay(url);
    }
    this.resetState();
  }

  dedupeRelayUrls(relays) {
    const out = [];
    const seen = new Set();

    for (const raw of Array.isArray(relays) ? relays : []) {
      const url = String(raw || "")
        .trim()
        .replace(/\/+$/, "");
      if (!url) continue;
      const key = url.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(url);
    }

    return out;
  }
}

export default RelayManager;
