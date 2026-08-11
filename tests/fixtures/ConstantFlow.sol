// SPDX-License-Identifier: MIT
pragma solidity ^0.4.25;

contract ConstantFlow {
    enum Mode { RUN, SKIP }

    function sink(Mode mode) internal {
        if (mode != Mode.SKIP) { }
    }

    function invoke() public {
        sink(Mode.SKIP);
    }
}
