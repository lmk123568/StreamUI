/**
 * WHEP(WebRTC) 播放器
 * 通过 ZLMediaKit 的 /index/api/whep 接口拉流播放
 *
 * 用法:
 *   const player = new WhepPlayer(videoElement, {
 *     onStatus: (state, text) => {},   // connecting | playing | reconnecting | error
 *     autoReconnect: true,
 *   });
 *   player.start(app, stream);
 *   player.stop();
 */
(function (global) {
    "use strict";

    // 未配置 STUN/TURN 时只有 host candidate，通常几毫秒即可收集完成；
    // 这里只作为兜底上限，避免个别浏览器收集过慢时把首屏拖到数秒
    const ICE_GATHER_TIMEOUT = 1000;
    const MAX_RETRY_DELAY = 15000;

    function getZlmBaseUrl() {
        return `http://${window.location.hostname}:8080`;
    }

    function WhepPlayer(videoEl, options) {
        options = options || {};

        this.videoEl = videoEl;
        this.baseUrl = options.baseUrl || getZlmBaseUrl();
        this.onStatus =
            typeof options.onStatus === "function" ? options.onStatus : function () { };
        this.autoReconnect = options.autoReconnect !== false;
        this.iceGatherTimeout = options.iceGatherTimeout || ICE_GATHER_TIMEOUT;

        this.app = null;
        this.stream = null;

        this.pc = null;
        this.sessionUrl = null;

        this.stopped = true;
        this.retryCount = 0;
        this.retryTimer = null;
        this.disconnectTimer = null;
        // 每次连接递增，用于丢弃过期的异步回调
        this.generation = 0;

        // 首屏耗时统计：发起连接时间 / 首帧渲染时间
        this.startedAt = 0;
        this.firstFrameAt = 0;
        this.playingEmitted = false;
    }

    WhepPlayer.getWhepUrl = function (app, stream, baseUrl) {
        const base = baseUrl || getZlmBaseUrl();
        return (
            base +
            "/index/api/whep?app=" +
            encodeURIComponent(app) +
            "&stream=" +
            encodeURIComponent(stream)
        );
    };

    WhepPlayer.prototype.emitStatus = function (state, text) {
        try {
            this.onStatus(state, text);
        } catch (e) {
            // 回调异常不影响播放
        }
    };

    WhepPlayer.prototype.start = function (app, stream) {
        if (app) this.app = app;
        if (stream) this.stream = stream;

        this.stop();

        if (!this.app || !this.stream || !this.videoEl) {
            this.emitStatus("error", "缺少流信息");
            return;
        }

        this.stopped = false;
        this.retryCount = 0;
        this.connect();
    };

    WhepPlayer.prototype.connect = function () {
        const self = this;
        const gen = ++this.generation;

        this.releasePeer();
        this.clearTimers();
        this.emitStatus("connecting", "连接中...");

        this.startedAt = Date.now();
        this.firstFrameAt = 0;
        this.playingEmitted = false;

        if (typeof global.RTCPeerConnection !== "function") {
            this.emitStatus("error", "当前浏览器不支持 WebRTC");
            return;
        }

        let pc;
        try {
            pc = new global.RTCPeerConnection({ iceServers: [] });
        } catch (err) {
            this.handleFailure(err);
            return;
        }
        this.pc = pc;

        // 只接收，不采集本地音视频
        pc.addTransceiver("video", { direction: "recvonly" });
        pc.addTransceiver("audio", { direction: "recvonly" });

        const fallbackStream =
            typeof global.MediaStream === "function" ? new global.MediaStream() : null;

        pc.ontrack = function (event) {
            if (gen !== self.generation || !self.videoEl) return;

            const remoteStream = event.streams && event.streams[0];
            if (remoteStream) {
                self.videoEl.srcObject = remoteStream;
            } else if (fallbackStream) {
                fallbackStream.addTrack(event.track);
                self.videoEl.srcObject = fallbackStream;
            }

            const p = self.videoEl.play();
            if (p && typeof p.catch === "function") {
                p.catch(function () { });
            }

            // 以"真正渲染出第一帧"作为播放开始，避免连接成功后仍然黑屏
            self.watchFirstFrame(gen);
        };

        pc.onconnectionstatechange = function () {
            if (gen !== self.generation || !self.pc) return;

            const state = pc.connectionState;
            if (state === "connected") {
                self.retryCount = 0;
                self.clearTimers();
                if (self.firstFrameAt) {
                    self.emitPlaying();
                } else {
                    self.emitStatus("waiting", "已连接，等待关键帧...");
                }
            } else if (state === "failed") {
                self.handleFailure(new Error("WebRTC 连接失败"));
            } else if (state === "disconnected") {
                self.scheduleDisconnectCheck(pc, gen);
            }
        };

        pc.createOffer()
            .then(function (offer) {
                return pc.setLocalDescription(offer);
            })
            .then(function () {
                // ZLMediaKit 不支持 trickle-ICE，需要等待候选收集完成
                return self.waitIceGathering(pc);
            })
            .then(function () {
                if (gen !== self.generation || !self.pc) return null;

                const url = WhepPlayer.getWhepUrl(self.app, self.stream, self.baseUrl);

                return fetch(url, {
                    method: "POST",
                    mode: "cors",
                    headers: { "Content-Type": "application/sdp" },
                    body: pc.localDescription.sdp,
                }).then(function (res) {
                    if (!res.ok) {
                        return res.text().then(function (text) {
                            throw new Error(
                                "WHEP " + res.status + (text ? ": " + text : "")
                            );
                        });
                    }

                    const location =
                        res.headers.get("Location") || res.headers.get("location");

                    return res.text().then(function (sdp) {
                        return { sdp: sdp, location: location };
                    });
                });
            })
            .then(function (result) {
                if (!result || gen !== self.generation || !self.pc) return;

                // CORS 未暴露 Location 时拿不到会话地址，仅影响主动销毁，不影响播放
                self.sessionUrl = result.location || null;

                return pc.setRemoteDescription({ type: "answer", sdp: result.sdp });
            })
            .catch(function (err) {
                if (gen !== self.generation) return;
                self.handleFailure(err);
            });
    };

    WhepPlayer.prototype.waitIceGathering = function (pc) {
        const self = this;

        return new Promise(function (resolve) {
            if (pc.iceGatheringState === "complete") {
                resolve();
                return;
            }

            let done = false;
            let timer = null;

            function finish() {
                if (done) return;
                done = true;
                if (timer) clearTimeout(timer);
                pc.removeEventListener("icegatheringstatechange", onChange);
                resolve();
            }

            function onChange() {
                if (pc.iceGatheringState === "complete") finish();
            }

            pc.addEventListener("icegatheringstatechange", onChange);
            timer = setTimeout(finish, self.iceGatherTimeout);
        });
    };

    // 监听首帧渲染：WebRTC 建立连接后仍需等上游 I 帧才能出画
    WhepPlayer.prototype.watchFirstFrame = function (gen) {
        const self = this;
        const video = this.videoEl;
        if (!video) return;

        if (typeof video.requestVideoFrameCallback === "function") {
            video.requestVideoFrameCallback(function () {
                self.onFirstFrame(gen);
            });
            return;
        }

        // 兼容不支持 requestVideoFrameCallback 的浏览器
        video.addEventListener("playing", function onPlaying() {
            video.removeEventListener("playing", onPlaying);
            self.onFirstFrame(gen);
        });
    };

    WhepPlayer.prototype.onFirstFrame = function (gen) {
        if (gen !== this.generation || this.firstFrameAt) return;

        this.firstFrameAt = Date.now();
        console.info(
            "[WhepPlayer] " +
            this.app +
            "/" +
            this.stream +
            " 首帧耗时 " +
            (this.firstFrameAt - this.startedAt) +
            "ms"
        );
        this.emitPlaying();
    };

    WhepPlayer.prototype.emitPlaying = function () {
        if (this.playingEmitted) return;
        this.playingEmitted = true;
        this.emitStatus("playing", "播放中");
    };

    WhepPlayer.prototype.scheduleDisconnectCheck = function (pc, gen) {
        const self = this;
        if (this.disconnectTimer !== null) return;

        this.disconnectTimer = setTimeout(function () {
            self.disconnectTimer = null;
            if (gen !== self.generation || self.stopped || !self.pc) return;
            if (pc.connectionState !== "connected") {
                self.handleFailure(new Error("WebRTC 连接断开"));
            }
        }, 3000);
    };

    WhepPlayer.prototype.handleFailure = function (err) {
        if (this.stopped) return;

        this.closeSession();
        this.releasePeer();

        if (!this.autoReconnect) {
            this.emitStatus("error", (err && err.message) || "播放失败");
            return;
        }

        if (this.retryTimer !== null) return;

        const retryIndex = Math.min(this.retryCount, 6);
        const delay =
            Math.min(MAX_RETRY_DELAY, 1000 * Math.pow(2, retryIndex)) +
            Math.floor(Math.random() * 300);
        this.retryCount += 1;

        this.emitStatus("reconnecting", "离线，重连中...");

        const self = this;
        this.retryTimer = setTimeout(function () {
            self.retryTimer = null;
            if (self.stopped) return;
            self.connect();
        }, delay);
    };

    WhepPlayer.prototype.stop = function () {
        this.stopped = true;
        this.generation++;
        this.retryCount = 0;
        this.firstFrameAt = 0;
        this.playingEmitted = false;
        this.clearTimers();

        this.closeSession();
        this.releasePeer();

        if (this.videoEl) {
            try {
                this.videoEl.pause();
            } catch (e) { }
            this.videoEl.srcObject = null;
            this.videoEl.removeAttribute("src");
            try {
                this.videoEl.load();
            } catch (e) { }
        }
    };

    WhepPlayer.prototype.isPlaying = function () {
        return !this.stopped && !!this.pc && this.pc.connectionState === "connected";
    };

    WhepPlayer.prototype.clearTimers = function () {
        if (this.retryTimer !== null) {
            clearTimeout(this.retryTimer);
            this.retryTimer = null;
        }
        if (this.disconnectTimer !== null) {
            clearTimeout(this.disconnectTimer);
            this.disconnectTimer = null;
        }
    };

    WhepPlayer.prototype.releasePeer = function () {
        const pc = this.pc;
        this.pc = null;
        if (!pc) return;

        pc.ontrack = null;
        pc.onconnectionstatechange = null;
        try {
            pc.close();
        } catch (e) { }
    };

    WhepPlayer.prototype.closeSession = function () {
        const url = this.sessionUrl;
        this.sessionUrl = null;
        if (!url) return;

        try {
            fetch(url, { method: "DELETE", mode: "cors", keepalive: true }).catch(
                function () { }
            );
        } catch (e) { }
    };

    global.WhepPlayer = WhepPlayer;
})(window);
