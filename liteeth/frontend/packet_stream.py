#
# This file is part of LiteEth.
#
# Copyright (c) 2015-2021 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from liteeth.common import *

# Steam 2 UDP TX -----------------------------------------------------------------------------------

class LiteEthStream2UDPTX(Module):
    # If no fifo is desired, the sink can't contain gaps during a data packet
    # If a fifo is used, the packet length should fit into the fifo
    def __init__(self, ip_address, udp_port, dw, fifo_depth=None):
        self.sink   = sink   = stream.Endpoint(eth_packet_stream_description(dw))
        self.source = source = stream.Endpoint(eth_udp_user_description(dw))

        # # #

        ip_address = convert_ip(ip_address)

        if fifo_depth is None:
            self.comb += [
                sink.connect(source),
                source.src_port.eq(udp_port),
                source.dst_port.eq(udp_port),
                source.ip_address.eq(ip_address),
            ]
        else:
            last_be_level   = Signal(max=fifo_depth+1)
            increase_last_be = Signal()
            decrease_last_be = Signal()
            self.underrun_error = Signal()
            self.submodules.fifo = fifo = stream.SyncFIFO([("data", dw), ("last_be", dw//8)], fifo_depth)
            self.comb += sink.connect(fifo.sink)

            self.submodules.fsm = fsm = FSM(reset_state="IDLE")
            fsm.act("IDLE",
                If((last_be_level != 0) | (fifo.level == (fifo_depth - 1)),
                   NextState("SEND")),
            )
            fsm.act("SEND",
                source.valid.eq(1),
                source.src_port.eq(udp_port),
                source.dst_port.eq(udp_port),
                source.ip_address.eq(ip_address),
                source.data.eq(fifo.source.data),
                If(source.ready,
                    fifo.source.ready.eq(1),
                    If(fifo.source.last_be != 0,
                        source.last_be.eq(fifo.source.last_be),
                        source.last.eq(1),
                        decrease_last_be.eq(1),
                        If(last_be_level == 1,
                            # In the same cycle a new last_be might be incoming, but a one cycle delay is alright
                            NextState("IDLE")),
                    # fifo is empty but no end yet in source.
                    # Insert new end
                    # TODO Signal underrun in a better way
                    ).Elif(fifo.level == 1,
                        self.underrun_error.eq(1),
                        source.last_be.eq(1 << (dw//8 - 1)),
                        source.last.eq(1),
                        NextState("IDLE")
                    )
                )
            )

            self.comb += [
                increase_last_be.eq((sink.last_be != 0) & fifo.sink.ready & fifo.sink.valid),
            ]
            self.sync += [
                If(increase_last_be & decrease_last_be
                ).Elif(increase_last_be,
                    last_be_level.eq(last_be_level + 1)
                ).Elif(decrease_last_be,
                    last_be_level.eq(last_be_level - 1))
            ]

# UDP to Stream RX ---------------------------------------------------------------------------------

class LiteEthUDP2StreamRX(Module):
    def __init__(self, ip_address, udp_port, dw, fifo_depth=None):
        self.sink   = sink   = stream.Endpoint(eth_udp_user_description(dw))
        self.source = source = stream.Endpoint(eth_packet_stream_description(dw))

        # # #

        ip_address = convert_ip(ip_address)

        valid = Signal()
        self.comb += valid.eq(
            (sink.ip_address == ip_address) &
            (sink.dst_port   == udp_port)
        )
        if fifo_depth is None:
            self.comb += [
                sink.connect(source, keep={"last", "ready", "data", "last_be"}),
                source.valid.eq(sink.valid & valid),
            ]
        else:
            self.submodules.fifo = fifo = stream.SyncFIFO([("data", dw), ("last_be", dw//8)], fifo_depth)
            self.comb += [
                sink.connect(fifo.sink, keep={"last", "ready", "data", "last_be"}),
                fifo.sink.valid.eq(sink.valid & valid),
                fifo.source.connect(source),
                source.last.eq(fifo.source.last_be != 0)
            ]

# UDP Streamer -------------------------------------------------------------------------------------

class LiteEthUDPStreamer(Module):
    def __init__(self, udp, ip_address, udp_port, dw, rx_fifo_depth=64, tx_fifo_depth=64):
        self.submodules.tx = tx = LiteEthStream2UDPTX(ip_address, udp_port, dw, tx_fifo_depth)
        self.submodules.rx = rx = LiteEthUDP2StreamRX(ip_address, udp_port, dw, rx_fifo_depth)
        udp_port = udp.crossbar.get_port(udp_port, dw)
        self.comb += [
            tx.source.connect(udp_port.sink),
            udp_port.source.connect(rx.sink)
        ]
        self.sink, self.source = self.tx.sink, self.rx.source
