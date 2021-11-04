#
# This file is part of LiteEth.
#
# Copyright (c) 2021 Leon Schuermann <leon@is.currently.online>
# SPDX-License-Identifier: BSD-2-Clause
#

import unittest
import random

from migen import *

from litex.soc.interconnect.stream import *
from liteeth.packet import *
from liteeth.frontend.packet_stream import LiteEthStream2UDPTX,LiteEthUDP2StreamRX

from .test_stream import StreamPacket, stream_inserter, stream_collector, compare_packets

class TestPacketStream(unittest.TestCase):
    def loopback_test(self, dw, seed=42, debug_print=True):
        # Independent random number generator to ensure we're the
        # stream_inserter and stream_collectors still have
        # reproducible behavior independent of the headers
        prng = random.Random(seed + 5)

        # Prepare packets
        npackets = 32
        packets  = []
        for n in range(npackets):
            # Due to being larger than the fifo, this *could* fail if the rand function works differently
            # More packets are received than sent, thus it hangs
            datas = [prng.randrange(2**8) for _ in range(prng.randrange(25 * dw//8) + 1)]
            packets.append(StreamPacket(datas))

        class DUT(Module):
            def __init__(self):
                self.submodules.udptx = LiteEthStream2UDPTX(
                    "192.168.1.1",
                    1234,
                    dw,
                    fifo_depth=16,
                )
                self.submodules.udprx = LiteEthUDP2StreamRX(
                    "192.168.1.1",
                    1234,
                    dw,
                    fifo_depth=16
                )
                self.comb += self.udptx.source.connect(self.udprx.sink)
                self.sink, self.source = self.udptx.sink, self.udprx.source

        dut = DUT()
        recvd_packets = []
        run_simulation(
            dut,
            [
                stream_inserter(
                    dut.sink,
                    src=packets,
                    seed=seed,
                    debug_print=debug_print,
                    valid_rand=50,
                ),
                stream_collector(
                    dut.source,
                    dest=recvd_packets,
                    expect_npackets=npackets,
                    seed=seed,
                    debug_print=debug_print,
                    ready_rand=50,
                ),
            ],
        )

    def test_32bit_loopback(self):
        for seed in range(42, 48):
            with self.subTest(seed=seed):
                self.loopback_test(dw=32, seed=seed)

    def test_16bit_loopback(self):
        for seed in range(42, 48):
            with self.subTest(seed=seed):
                self.loopback_test(dw=16, seed=seed)
