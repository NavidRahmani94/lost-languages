// =====================================================================
//  LOST LANGUAGES — sound engine
//  Every language has a music before it has a meaning.
//
//  HOW TO RUN: place the cursor anywhere inside the big ( ... ) block
//  below and press Cmd+Enter (macOS) / Ctrl+Enter (Win/Linux).
//  Then run the Python script. Stop everything with Cmd/Ctrl + .
//
//  Listens on the default sclang port 57120:
//    /ll/start  path dur rootMidi direction label
//    /ll/note   midi amp dur bright pan
//    /ll/noise  amp centerHz dur pan
//    /ll/end
// =====================================================================

(
// The desktop app starts this file in the background and waits for /ll/ready.
// Ports are chosen by the app (environment variables); defaults for the IDE.
~llReply = NetAddr("127.0.0.1", ("LL_REPLY_PORT".getenv ? "57300").asInteger);
// A dedicated server, so it never clashes with a server already running in the IDE.
Server.default = s = Server(\lostLanguages,
    NetAddr("127.0.0.1", ("LL_SERVER_PORT".getenv ? "57190").asInteger));
s.options.numInputBusChannels = 0;     // the app records the mic itself
s.options.numOutputBusChannels = 2;

s.waitForBoot({

    // ------------------------------------------------------------------
    // 1. SYNTH DEFINITIONS
    // ------------------------------------------------------------------

    // Melodic voice: a soft FM bell. Brightness of the spoken syllable
    // controls the modulation index and the filter.
    SynthDef(\llTone, { |out = 0, freq = 440, amp = 0.2, dur = 0.4, bright = 0.5, pan = 0|
        var env, modEnv, mod, sig;
        env    = Env.perc(0.008, dur * 2.2, 1, -4).kr(doneAction: 2);
        modEnv = Env.perc(0.005, dur * 0.8).kr;
        mod    = SinOsc.ar(freq * 2.0, 0, freq * (0.3 + (bright * 2.5)) * modEnv);
        sig    = SinOsc.ar(freq + mod) * 0.7
               + (SinOsc.ar(freq * 0.5) * 0.25 * env);           // warm sub-octave
        sig    = LPF.ar(sig, bright.linexp(0, 1, 1800, 9000));
        Out.ar(out, Pan2.ar(sig * env * amp, pan));
    }).add;

    // Consonants / breath: band-passed noise, like a pen scratching paper.
    SynthDef(\llNoise, { |out = 0, amp = 0.1, cf = 4000, dur = 0.2, pan = 0|
        var env = Env.linen(0.01, dur * 0.6, dur * 0.4, 1, \sine).kr(doneAction: 2);
        var sig = BPF.ar(PinkNoise.ar(1), cf.clip(400, 12000), 0.25) * 3;
        Out.ar(out, Pan2.ar(sig * env * amp, pan));
    }).add;

    // Drone on the root of the speaker's mode: the "ground" of the language.
    SynthDef(\llDrone, { |out = 0, freq = 110, amp = 0.07, gate = 1|
        var env = Env.asr(2.5, 1, 4).kr(doneAction: 2, gate: gate);
        var sig = Mix(SinOsc.ar(freq * [1, 1.003, 1.5, 2, 2.997], 0, [0.4, 0.4, 0.2, 0.15, 0.08]));
        sig = LPF.ar(sig, LFNoise1.kr(0.2).range(500, 1400));
        Out.ar(out, (sig * env * amp) ! 2);
    }).add;

    // Granular voice: the real voice of the speaker, shattered into tiny
    // grains. You hear WHO is speaking, but no longer WHAT they say.
    SynthDef(\llGrain, { |out = 0, buf, amp = 0.18, dur = 5, gate = 1|
        var env  = Env.asr(0.6, 1, 2.5).kr(doneAction: 2, gate: gate);
        var pos  = Line.kr(0, 1, dur) + LFNoise1.kr(4).range(-0.015, 0.015);
        var trig = Dust.kr(28);
        var sig  = GrainBuf.ar(2, trig, TRand.kr(0.06, 0.18, trig), buf,
                               TRand.kr(0.98, 1.02, trig), pos.clip(0, 1), 2,
                               TRand.kr(-0.8, 0.8, trig));
        Out.ar(out, sig * env * amp);
    }).add;

    // Shared space: reverb on everything.
    SynthDef(\llVerb, { |in, out = 0, mix = 0.38, room = 0.85, master = 1|
        var sig = In.ar(in, 2);
        sig = FreeVerb2.ar(sig[0], sig[1], mix.lag(0.1), room, 0.4);
        Out.ar(out, Limiter.ar(sig * master.lag(0.1), 0.95));
    }).add;

    s.sync;

    // ------------------------------------------------------------------
    // 2. ROUTING
    // ------------------------------------------------------------------
    ~bus = Bus.audio(s, 2);
    ~fx  = Synth(\llVerb, [\in, ~bus], s, \addToTail);   // after all sources

    ~drone = nil; ~grain = nil; ~buf = nil;
    // output mixer (set from the app's "Input & Mixer" tab)
    ~mix = (tone: 1, noise: 1, drone: 1, grain: 1, reverb: 0.38, master: 1);

    ~release = {
        ~drone !? { ~drone.set(\gate, 0) };
        ~grain !? { ~grain.set(\gate, 0) };
        ~buf   !? { var b = ~buf; SystemClock.sched(5, { b.free; nil }) };
        ~drone = nil; ~grain = nil; ~buf = nil;
    // output mixer (set from the app's "Input & Mixer" tab)
    ~mix = (tone: 1, noise: 1, drone: 1, grain: 1, reverb: 0.38, master: 1);
    };

    // ------------------------------------------------------------------
    // 3. OSC
    // ------------------------------------------------------------------
    OSCdef(\llStart, { |msg|
        var path = msg[1].asString, dur = msg[2], root = msg[3], label = msg[5];
        ("▶ " ++ label ++ "  root " ++ root ++ "  " ++ dur.round(0.1) ++ "s").postln;
        ~release.value;
        ~drone = Synth(\llDrone, [\out, ~bus, \freq, (root - 24).midicps, \amp, 0.07 * ~mix[\drone]]);
        Buffer.readChannel(s, path, channels: [0], action: { |b|
            ~buf = b;
            ~grain = Synth(\llGrain, [\out, ~bus, \buf, b, \dur, dur, \amp, 0.18 * ~mix[\grain]]);
        });
    }, '/ll/start');

    OSCdef(\llNote, { |msg|
        Synth(\llTone, [\out, ~bus, \freq, msg[1].midicps, \amp, msg[2] * ~mix[\tone],
                        \dur, msg[3], \bright, msg[4], \pan, msg[5]]);
    }, '/ll/note');

    OSCdef(\llNoise, { |msg|
        Synth(\llNoise, [\out, ~bus, \amp, msg[1] * ~mix[\noise], \cf, msg[2],
                         \dur, msg[3], \pan, msg[4]]);
    }, '/ll/noise');

    OSCdef(\llEnd, { |msg|
        "■ end".postln;
        ~release.value;
    }, '/ll/end');

    OSCdef(\llMix, { |msg|
        var k = msg[1].asSymbol, v = msg[2].asFloat;
        ~mix[k] = v;
        case
        { k == \reverb } { ~fx.set(\mix, v) }
        { k == \master } { ~fx.set(\master, v) }
        { k == \drone }  { ~drone !? { ~drone.set(\amp, 0.07 * v) } }
        { k == \grain }  { ~grain !? { ~grain.set(\amp, 0.18 * v) } };
    }, '/ll/mix');

    OSCdef(\llPing, { ~llReply.sendMsg('/ll/ready', NetAddr.langPort) }, '/ll/ping');

    OSCdef(\llQuit, {
        "quitting".postln;
        s.quit;
        AppClock.sched(1.5, { 0.exit; nil });
    }, '/ll/quit');

    "LOST LANGUAGES sound engine ready — listening on port %".format(NetAddr.langPort).postln;
    ~llReply.sendMsg('/ll/ready', NetAddr.langPort);
});
)

// --- quick test without Python: evaluate these lines one by one -----
// Synth(\llTone, [\out, ~bus, \freq, 67.midicps, \amp, 0.3, \dur, 0.5, \bright, 0.6]);
// Synth(\llNoise, [\out, ~bus, \amp, 0.2, \cf, 5000, \dur, 0.3]);
// x = Synth(\llDrone, [\out, ~bus, \freq, 43.midicps]); x.set(\gate, 0);
