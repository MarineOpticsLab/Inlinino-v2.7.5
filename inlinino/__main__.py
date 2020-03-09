# -*- coding: utf-8 -*-
# @Author: nils
# @Date:   2016-05-22 17:21:16
# @Last Modified by:   nils
# @Last Modified time: 2016-06-27 11:08:50

import os
import sys

from inlinino import Inlinino


if len(sys.argv) == 2:
    Inlinino(sys.argv[1])
else:
    Inlinino(os.path.join(sys.path[0], 'cfg', 'simulino_cfg.json'))
