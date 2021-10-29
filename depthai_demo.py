#!/usr/bin/env python3
import os
import queue
import threading
import time
from contextlib import ExitStack
from functools import cmp_to_key
from itertools import cycle
from pathlib import Path

import cv2
import depthai as dai
import platform

from depthai_helpers.arg_manager import parseArgs
from depthai_helpers.config_manager import ConfigManager, DEPTHAI_ZOO, DEPTHAI_VIDEOS
from depthai_helpers.version_check import checkRequirementsVersion
from depthai_sdk import FPSHandler, loadModule, getDeviceInfo, downloadYTVideo, Previews, resizeLetterbox
from depthai_sdk.managers import NNetManager, PreviewManager, PipelineManager, EncodingManager, BlobManager

print('Using depthai module from: ', dai.__file__)
print('Depthai version installed: ', dai.__version__)
if platform.machine() not in ['armv6l', 'aarch64']:
    checkRequirementsVersion()

confManager = ConfigManager(parseArgs())
confManager.linuxCheckApplyUsbRules()
if not confManager.useCamera:
    if str(confManager.args.video).startswith('https'):
        confManager.args.video = downloadYTVideo(confManager.args.video, DEPTHAI_VIDEOS)
        print("Youtube video downloaded.")
    if not Path(confManager.args.video).exists():
        raise ValueError("Path {} does not exists!".format(confManager.args.video))


class Trackbars:
    instances = {}

    @staticmethod
    def createTrackbar(name, window, minVal, maxVal, defaultVal, callback):
        def fn(value):
            if Trackbars.instances[name][window] != value:
                callback(value)
            for otherWindow, previousValue in Trackbars.instances[name].items():
                if otherWindow != window and previousValue != value:
                    Trackbars.instances[name][otherWindow] = value
                    cv2.setTrackbarPos(name, otherWindow, value)

        cv2.createTrackbar(name, window, minVal, maxVal, fn)
        Trackbars.instances[name] = {**Trackbars.instances.get(name, {}), window: defaultVal}
        cv2.setTrackbarPos(name, window, defaultVal)


noop = lambda *a, **k: None


class Demo:
    DISP_CONF_MIN = int(os.getenv("DISP_CONF_MIN", 0))
    DISP_CONF_MAX = int(os.getenv("DISP_CONF_MAX", 255))
    SIGMA_MIN = int(os.getenv("SIGMA_MIN", 0))
    SIGMA_MAX = int(os.getenv("SIGMA_MAX", 250))
    LRCT_MIN = int(os.getenv("LRCT_MIN", 0))
    LRCT_MAX = int(os.getenv("LRCT_MAX", 10))

    def run_all(self):
        self.setup()
        self.run()

    def __init__(self, conf: ConfigManager, displayFrames=True, onNewFrame = noop, onShowFrame = noop, onNn = noop, onReport = noop, onSetup = noop, onTeardown = noop, onIter = noop, shouldRun = lambda: True):
        self._conf = conf
        self._rgbRes = conf.getRgbResolution()
        self._monoRes = conf.getMonoResolution()
        self._openvinoVersion = None
        if conf.args.openvinoVersion:
            self._openvinoVersion = getattr(dai.OpenVINO.Version, 'VERSION_' + conf.args.openvinoVersion)
        self._displayFrames = displayFrames

        self.onNewFrame = onNewFrame
        self.onShowFrame = onShowFrame
        self.onNn = onNn
        self.onReport = onReport
        self.onSetup = onSetup
        self.onTeardown = onTeardown
        self.onIter = onIter
        self.shouldRun = shouldRun
    
    def setCallbacks(self, onNewFrame=None, onShowFrame=None, onNn=None, onReport=None, onSetup=None, onTeardown=None, onIter=None, shouldRun=None):
        if onNewFrame is not None:
            self.onNewFrame = onNewFrame
        if onShowFrame is not None:
            self.onShowFrame = onShowFrame
        if onNn is not None:
            self.onNn = onNn
        if onReport is not None:
            self.onReport = onReport
        if onSetup is not None:
            self.onSetup = onSetup
        if onTeardown is not None:
            self.onTeardown = onTeardown
        if onIter is not None:
            self.onIter = onIter
        if shouldRun is not None:
            self.shouldRun = shouldRun


    def setup(self):
        print("Setting up demo...")
        self._stack = ExitStack()
        self._deviceInfo = getDeviceInfo(self._conf.args.deviceId)
        if self._conf.args.reportFile:
            reportFileP = Path(self._conf.args.reportFile).with_suffix('.csv')
            reportFileP.parent.mkdir(parents=True, exist_ok=True)
            self._reportFile = self._stack.enter_context(reportFileP.open('a'))
        self._pm = PipelineManager(self._openvinoVersion)

        if self._conf.args.xlinkChunkSize is not None:
            self._pm.setXlinkChunkSize(self._conf.args.xlinkChunkSize)

        if self._conf.useNN:
            self._blobManager = BlobManager(
                zooDir=DEPTHAI_ZOO,
                zooName=self._conf.getModelName(),
            )
            self._nnManager = NNetManager(inputSize=self._conf.inputSize)

            if self._conf.getModelDir() is not None:
                configPath = self._conf.getModelDir() / Path(self._conf.getModelName()).with_suffix(f".json")
                self._nnManager.readConfig(configPath)

            self._nnManager.countLabel(self._conf.getCountLabel(self._nnManager))
            self._pm.setNnManager(self._nnManager)

        self._device = self._stack.enter_context(
            dai.Device(self._pm.pipeline.getOpenVINOVersion(), self._deviceInfo, usb2Mode=self._conf.args.usbSpeed == "usb2")
        )
        if self._deviceInfo.desc.protocol == dai.XLinkProtocol.X_LINK_USB_VSC:
            print("USB Connection speed: {}".format(self._device.getUsbSpeed()))
        self._conf.adjustParamsToDevice(self._device)
        self._conf.adjustPreviewToOptions()
        if self._conf.lowBandwidth:
            self._pm.enableLowBandwidth()
        self._cap = cv2.VideoCapture(self._conf.args.video) if not self._conf.useCamera else None
        self._fps = FPSHandler() if self._conf.useCamera else FPSHandler(self._cap)

        if self._conf.useCamera or self._conf.args.sync:
            self._pv = PreviewManager(display=self._conf.args.show, nnSource=self._conf.getModelSource(), colorMap=self._conf.getColorMap(),
                                dispMultiplier=self._conf.dispMultiplier, mouseTracker=True, lowBandwidth=self._conf.lowBandwidth,
                                scale=self._conf.args.scale, sync=self._conf.args.sync, fpsHandler=self._fps, createWindows=self._displayFrames)

            if self._conf.leftCameraEnabled:
                self._pm.createLeftCam(self._monoRes, self._conf.args.monoFps,
                                 orientation=self._conf.args.cameraOrientation.get(Previews.left.name),
                                 xout=Previews.left.name in self._conf.args.show and (self._conf.getModelSource() != "left" or not self._conf.args.sync))
            if self._conf.rightCameraEnabled:
                self._pm.createRightCam(self._monoRes, self._conf.args.monoFps,
                                  orientation=self._conf.args.cameraOrientation.get(Previews.right.name),
                                  xout=Previews.right.name in self._conf.args.show and (self._conf.getModelSource() != "right" or not self._conf.args.sync))
            if self._conf.rgbCameraEnabled:
                self._pm.createColorCam(self._nnManager.inputSize if self._conf.useNN else self._conf.previewSize, self._rgbRes, self._conf.args.rgbFps,
                                  orientation=self._conf.args.cameraOrientation.get(Previews.color.name),
                                  fullFov=not self._conf.args.disableFullFovNn,
                                  xout=Previews.color.name in self._conf.args.show and (self._conf.getModelSource() != "color" or not self._conf.args.sync))

            if self._conf.useDepth:
                self._pm.createDepth(
                    self._conf.args.disparityConfidenceThreshold,
                    self._conf.getMedianFilter(),
                    self._conf.args.sigma,
                    self._conf.args.stereoLrCheck,
                    self._conf.args.lrcThreshold,
                    self._conf.args.extendedDisparity,
                    self._conf.args.subpixel,
                    useDepth=Previews.depth.name in self._conf.args.show or Previews.depthRaw.name in self._conf.args.show,
                    useDisparity=Previews.disparity.name in self._conf.args.show or Previews.disparityColor.name in self._conf.args.show,
                    useRectifiedLeft=Previews.rectifiedLeft.name in self._conf.args.show and (
                                self._conf.getModelSource() != "rectifiedLeft" or not self._conf.args.sync),
                    useRectifiedRight=Previews.rectifiedRight.name in self._conf.args.show and (
                                self._conf.getModelSource() != "rectifiedRight" or not self._conf.args.sync),
                )

            self._encManager = None
            if len(self._conf.args.encode) > 1:
                self._encManager = EncodingManager(self._conf.args.encode, self._conf.args.encodeOutput)
                self._encManager.createEncoders(self._pm)

        if len(self._conf.args.report) > 0:
            self._pm.createSystemLogger()

        if self._conf.useNN:
            self._nn = self._nnManager.createNN(
                pipeline=self._pm.pipeline, nodes=self._pm.nodes, source=self._conf.getModelSource(),
                blobPath=self._blobManager.getBlob(shaves=self._conf.shaves, openvinoVersion=self._nnManager.openvinoVersion),
                useDepth=self._conf.useDepth, minDepth=self._conf.args.minDepth, maxDepth=self._conf.args.maxDepth,
                sbbScaleFactor=self._conf.args.sbbScaleFactor, fullFov=not self._conf.args.disableFullFovNn,
                flipDetection=self._conf.getModelSource() in (
                "rectifiedLeft", "rectifiedRight") and not self._conf.args.stereoLrCheck,
            )

            self._pm.addNn(
                nn=self._nn, sync=self._conf.args.sync, xoutNnInput=Previews.nnInput.name in self._conf.args.show,
                useDepth=self._conf.useDepth, xoutSbb=self._conf.args.spatialBoundingBox and self._conf.useDepth
            )

    def run(self):
        self._device.startPipeline(self._pm.pipeline)
        self._pm.createDefaultQueues(self._device)
        if self._conf.useNN:
            self._nnManager.createQueues(self._device)

        self._sbbOut = self._device.getOutputQueue("sbb", maxSize=1, blocking=False) if self._conf.useNN and self._conf.args.spatialBoundingBox else None
        self._logOut = self._device.getOutputQueue("systemLogger", maxSize=30, blocking=False) if len(self._conf.args.report) > 0 else None

        self._medianFilters = cycle([item for name, item in vars(dai.MedianFilter).items() if name.startswith('KERNEL_') or name.startswith('MEDIAN_')])
        for medFilter in self._medianFilters:
            # move the cycle to the current median filter
            if medFilter == self._pm._depthConfig.getMedianFilter():
                break

        if self._conf.useCamera:
            cameras = self._device.getConnectedCameras()
            if dai.CameraBoardSocket.LEFT in cameras and dai.CameraBoardSocket.RIGHT in cameras:
                self._pv.collectCalibData(self._device)

            self._cameraConfig = {
                "exposure": self._conf.args.cameraExposure,
                "sensitivity": self._conf.args.cameraSensitivity,
                "saturation": self._conf.args.cameraSaturation,
                "contrast": self._conf.args.cameraContrast,
                "brightness": self._conf.args.cameraBrightness,
                "sharpness": self._conf.args.cameraSharpness
            }

            if any(self._cameraConfig.values()):
                self._updateCameraConfigs()

            self._pv.createQueues(self._device, self._createQueueCallback)
            if self._encManager is not None:
                self._encManager.createDefaultQueues(self._device)
        elif self._conf.args.sync:
            self._hostOut = self._device.getOutputQueue(Previews.nnInput.name, maxSize=1, blocking=False)

        self._seqNum = 0
        self._hostFrame = None
        self._nnData = []
        self._sbbRois = []
        self.onSetup(self)

        try:
            while self.shouldRun():
                self._fps.nextIter()
                self.onIter(self)
                self.loop()
        except StopIteration:
            pass
        finally:
            self.stop()

    def stop(self):
        print("Stopping demo...")
        self._device.close()
        self._stack.close()
        self._pm.closeDefaultQueues()
        if self._conf.useCamera:
            self._pv.closeQueues()
            if self._encManager is not None:
                self._encManager.close()
        if self._conf.useNN:
            self._nnManager.closeQueues()
        if self._sbbOut is not None:
            self._sbbOut.close()
        if self._logOut is not None:
            self._logOut.close()
        self._fps.printStatus()
        self.onTeardown(self)


    def loop(self):
        if self._conf.useCamera:
            self._pv.prepareFrames(callback=self.onNewFrame)
            if self._encManager is not None:
                self._encManager.parseQueues()

            if self._sbbOut is not None:
                sbb = self._sbbOut.tryGet()
                if sbb is not None:
                    self._sbbRois = sbb.getConfigData()
                depthFrames = [self._pv.get(Previews.depthRaw.name), self._pv.get(Previews.depth.name)]
                for depthFrame in depthFrames:
                    if depthFrame is None:
                        continue

                    for roiData in self._sbbRois:
                        roi = roiData.roi.denormalize(depthFrame.shape[1], depthFrame.shape[0])
                        topLeft = roi.topLeft()
                        bottomRight = roi.bottomRight()
                        # Display SBB on the disparity map
                        cv2.rectangle(depthFrame, (int(topLeft.x), int(topLeft.y)), (int(bottomRight.x), int(bottomRight.y)), self._nnManager._bboxColors[0], 2)
        else:
            readCorrectly, rawHostFrame = self._cap.read()
            if not readCorrectly:
                raise StopIteration()

            self._nnManager.sendInputFrame(rawHostFrame, self._seqNum)
            self._seqNum += 1

            if not self._conf.args.sync:
                self._hostFrame = rawHostFrame
            self._fps.tick('host')

        if self._conf.useNN:
            inNn = self._nnManager.outputQueue.tryGet()
            if inNn is not None:
                self.onNn(inNn)
                if not self._conf.useCamera and self._conf.args.sync:
                    self._hostFrame = Previews.nnInput.value(self._hostOut.get())
                self._nnData = self._nnManager.decode(inNn)
                self._fps.tick('nn')

        if self._conf.useCamera:
            if self._conf.useNN:
                self._nnManager.draw(self._pv, self._nnData)
                self._pv.showFrames(callback=self._showFramesCallback)
        elif self._hostFrame is not None:
            debugHostFrame = self._hostFrame.copy()
            if self._conf.useNN:
                self._nnManager.draw(debugHostFrame, self._nnData)
            self._fps.drawFps(debugHostFrame, "host")
            if self._displayFrames:
                cv2.imshow("host", debugHostFrame)

        if self._logOut:
            logs = self._logOut.tryGetAll()
            for log in logs:
                self._printSysInfo(log)

        if self._displayFrames:
            key = cv2.waitKey(1)
            if key == ord('q'):
                raise StopIteration()
            elif key == ord('m'):
                nextFilter = next(self._medianFilters)
                self._pm.updateDepthConfig(self._device, median=nextFilter)

            if self._conf.args.cameraControlls:
                update = True

                if key == ord('t'):
                    self._cameraConfig["exposure"] = 10000 if self._cameraConfig["exposure"] is None else 500 if self._cameraConfig["exposure"] == 1 else min(self._cameraConfig["exposure"] + 500, 33000)
                    if self._cameraConfig["sensitivity"] is None:
                        self._cameraConfig["sensitivity"] = 800
                elif key == ord('g'):
                    self._cameraConfig["exposure"] = 10000 if self._cameraConfig["exposure"] is None else max(self._cameraConfig["exposure"] - 500, 1)
                    if self._cameraConfig["sensitivity"] is None:
                        self._cameraConfig["sensitivity"] = 800
                elif key == ord('y'):
                    self._cameraConfig["sensitivity"] = 800 if self._cameraConfig["sensitivity"] is None else min(self._cameraConfig["sensitivity"] + 50, 1600)
                    if self._cameraConfig["exposure"] is None:
                        self._cameraConfig["exposure"] = 10000
                elif key == ord('h'):
                    self._cameraConfig["sensitivity"] = 800 if self._cameraConfig["sensitivity"] is None else max(self._cameraConfig["sensitivity"] - 50, 100)
                    if self._cameraConfig["exposure"] is None:
                        self._cameraConfig["exposure"] = 10000
                elif key == ord('u'):
                    self._cameraConfig["saturation"] = 0 if self._cameraConfig["saturation"] is None else min(self._cameraConfig["saturation"] + 1, 10)
                elif key == ord('j'):
                    self._cameraConfig["saturation"] = 0 if self._cameraConfig["saturation"] is None else max(self._cameraConfig["saturation"] - 1, -10)
                elif key == ord('i'):
                    self._cameraConfig["contrast"] = 0 if self._cameraConfig["contrast"] is None else min(self._cameraConfig["contrast"] + 1, 10)
                elif key == ord('k'):
                    self._cameraConfig["contrast"] = 0 if self._cameraConfig["contrast"] is None else max(self._cameraConfig["contrast"] - 1, -10)
                elif key == ord('o'):
                    self._cameraConfig["brightness"] = 0 if self._cameraConfig["brightness"] is None else min(self._cameraConfig["brightness"] + 1, 10)
                elif key == ord('l'):
                    self._cameraConfig["brightness"] = 0 if self._cameraConfig["brightness"] is None else max(self._cameraConfig["brightness"] - 1, -10)
                elif key == ord('p'):
                    self._cameraConfig["sharpness"] = 0 if self._cameraConfig["sharpness"] is None else min(self._cameraConfig["sharpness"] + 1, 4)
                elif key == ord(';'):
                    self._cameraConfig["sharpness"] = 0 if self._cameraConfig["sharpness"] is None else max(self._cameraConfig["sharpness"] - 1, 0)
                else:
                    update = False

                if update:
                    self._updateCameraConfigs()

    def _createQueueCallback(self, queueName):
        if self._displayFrames and queueName in [Previews.disparityColor.name, Previews.disparity.name, Previews.depth.name, Previews.depthRaw.name]:
            Trackbars.createTrackbar('Disparity confidence', queueName, self.DISP_CONF_MIN, self.DISP_CONF_MAX, self._conf.args.disparityConfidenceThreshold,
                     lambda value: self._pm.updateDepthConfig(self._device, dct=value))
            if queueName in [Previews.depthRaw.name, Previews.depth.name]:
                Trackbars.createTrackbar('Bilateral sigma', queueName, self.SIGMA_MIN, self.SIGMA_MAX, self._conf.args.sigma,
                         lambda value: self._pm.updateDepthConfig(self._device, sigma=value))
            if self._conf.args.stereoLrCheck:
                Trackbars.createTrackbar('LR-check threshold', queueName, self.LRCT_MIN, self.LRCT_MAX, self._conf.args.lrcThreshold,
                         lambda value: self._pm.updateDepthConfig(self._device, lrcThreshold=value))

    def _updateCameraConfigs(self):
        if self._conf.leftCameraEnabled:
            self._pm.updateLeftCamConfig(self._device, **self._cameraConfig)
        if self._conf.rightCameraEnabled:
            self._pm.updateRightCamConfig(self._device, **self._cameraConfig)
        if self._conf.rgbCameraEnabled:
            self._pm.updateColorCamConfig(self._device, **self._cameraConfig)

    def _showFramesCallback(self, frame, name):
        self._fps.drawFps(frame, name)
        h, w = frame.shape[:2]
        if name in [Previews.disparityColor.name, Previews.disparity.name, Previews.depth.name, Previews.depthRaw.name]:
            text = "Median filter: {} [M]".format(self._pm._depthConfig.getMedianFilter().name.lstrip("KERNEL_").lstrip("MEDIAN_"))
            cv2.putText(frame, text, (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 4)
            cv2.putText(frame, text, (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 255, 1)
        elif self._conf.args.cameraControlls and name in [Previews.color.name, Previews.left.name, Previews.right.name]:
            text = "Exposure: {}   T [+] [-] G".format(self._cameraConfig["exposure"] if self._cameraConfig["exposure"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 110), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 110), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
            text = "Sensitivity: {}   Y [+] [-] H".format(self._cameraConfig["sensitivity"] if self._cameraConfig["sensitivity"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 90), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 90), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
            text = "Saturation: {}   U [+] [-] J".format(self._cameraConfig["saturation"] if self._cameraConfig["saturation"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 70), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 70), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
            text = "Contrast: {}   I [+] [-] K".format(self._cameraConfig["contrast"] if self._cameraConfig["contrast"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 50), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 50), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
            text = "Brightness: {}   O [+] [-] L".format(self._cameraConfig["brightness"] if self._cameraConfig["brightness"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 30), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 30), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
            text = "Sharpness: {}   P [+] [-] ;".format(self._cameraConfig["sharpness"] if self._cameraConfig["sharpness"] is not None else "auto")
            label_width = cv2.getTextSize(text, cv2.FONT_HERSHEY_TRIPLEX, 0.5, 4)[0][0]
            cv2.putText(frame, text, (w - label_width, h - 10), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4)
            cv2.putText(frame, text, (w - label_width, h - 10), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1)
        returnFrame = self.onShowFrame(frame, name)
        return returnFrame if returnFrame is not None else frame


    def _printSysInfo(self, info):
        m = 1024 * 1024 # MiB
        if not self._conf.args.reportFile:
            if "memory" in self._conf.args.report:
                print(f"Drr used / total - {info.ddrMemoryUsage.used / m:.2f} / {info.ddrMemoryUsage.total / m:.2f} MiB")
                print(f"Cmx used / total - {info.cmxMemoryUsage.used / m:.2f} / {info.cmxMemoryUsage.total / m:.2f} MiB")
                print(f"LeonCss heap used / total - {info.leonCssMemoryUsage.used / m:.2f} / {info.leonCssMemoryUsage.total / m:.2f} MiB")
                print(f"LeonMss heap used / total - {info.leonMssMemoryUsage.used / m:.2f} / {info.leonMssMemoryUsage.total / m:.2f} MiB")
            if "temp" in self._conf.args.report:
                t = info.chipTemperature
                print(f"Chip temperature - average: {t.average:.2f}, css: {t.css:.2f}, mss: {t.mss:.2f}, upa0: {t.upa:.2f}, upa1: {t.dss:.2f}")
            if "cpu" in self._conf.args.report:
                print(f"Cpu usage - Leon OS: {info.leonCssCpuUsage.average * 100:.2f}%, Leon RT: {info.leonMssCpuUsage.average * 100:.2f} %")
            print("----------------------------------------")
        else:
            data = {}
            if "memory" in self._conf.args.report:
                data = {
                    **data,
                    "ddrUsed": info.ddrMemoryUsage.used,
                    "ddrTotal": info.ddrMemoryUsage.total,
                    "cmxUsed": info.cmxMemoryUsage.used,
                    "cmxTotal": info.cmxMemoryUsage.total,
                    "leonCssUsed": info.leonCssMemoryUsage.used,
                    "leonCssTotal": info.leonCssMemoryUsage.total,
                    "leonMssUsed": info.leonMssMemoryUsage.used,
                    "leonMssTotal": info.leonMssMemoryUsage.total,
                }
            if "temp" in self._conf.args.report:
                data = {
                    **data,
                    "tempAvg": info.chipTemperature.average,
                    "tempCss": info.chipTemperature.css,
                    "tempMss": info.chipTemperature.mss,
                    "tempUpa0": info.chipTemperature.upa,
                    "tempUpa1": info.chipTemperature.dss,
                }
            if "cpu" in self._conf.args.report:
                data = {
                    **data,
                    "cpuCssAvg": info.leonCssCpuUsage.average,
                    "cpuMssAvg": info.leonMssCpuUsage.average,
                }

            if self._reportFile.tell() == 0:
                print(','.join(data.keys()), file=self._reportFile)
            self.onReport(data)
            print(','.join(map(str, data.values())), file=self._reportFile)



if __name__ == "__main__":
    from gui.main import DemoQtGui
    from PySide6.QtGui import QImage

    from PySide6.QtCore import QRunnable, Slot, QThreadPool, QObject, Signal


    class WorkerSignals(QObject):
        updatePreviewSignal = Signal(QImage)
        setDataSignal = Signal(list)
        exitSignal = Signal()

    class Worker(QRunnable):
        def __init__(self, instance, selectedPreview=None):
            super(Worker, self).__init__()
            self.running = False
            self.selectedPreview = selectedPreview
            self.instance = instance
            self.signals = WorkerSignals()
            self.signals.exitSignal.connect(self.terminate)

        @Slot()
        def run(self):
            self.running = True
            self.signals.setDataSignal.emit(["restartRequired", False])
            self.instance.setCallbacks(shouldRun=self.shouldRun, onShowFrame=self.onShowFrame, onSetup=self.onSetup)
            self.instance.run_all()

        @Slot()
        def terminate(self):
            self.running = False
            self.signals.setDataSignal.emit(["restartRequired", False])


        def shouldRun(self):
            return self.running

        def onShowFrame(self, frame, source):
            if source == self.selectedPreview:
                scaledFrame = resizeLetterbox(frame, (560, 560))
                if len(frame.shape) == 3:
                    img = QImage(scaledFrame.data, 560, 560, frame.shape[2] * 560, QImage.Format_BGR888)
                else:
                    img = QImage(scaledFrame.data, 560, 560, 560, QImage.Format_Grayscale8)
                self.signals.updatePreviewSignal.emit(img)

        def onSetup(self, instance):
            medianChoices = list(filter(lambda name: name.startswith('KERNEL_') or name.startswith('MEDIAN_'), vars(dai.MedianFilter).keys()))[::-1]
            self.signals.setDataSignal.emit(["medianChoices", medianChoices])
            colorChoices = list(filter(lambda name: name[0].isupper(), vars(dai.ColorCameraProperties.SensorResolution).keys()))
            self.signals.setDataSignal.emit(["colorResolutionChoices", colorChoices])
            monoChoices = list(filter(lambda name: name[0].isupper(), vars(dai.MonoCameraProperties.SensorResolution).keys()))
            self.signals.setDataSignal.emit(["monoResolutionChoices", monoChoices])
            self.signals.setDataSignal.emit(["previewChoices", confManager.args.show])
            self.signals.setDataSignal.emit(["modelSourceChoices", [Previews.color.name, Previews.left.name, Previews.right.name]])
            versionChoices = list(filter(lambda name: name.startswith("VERSION_"), vars(dai.OpenVINO).keys()))
            self.signals.setDataSignal.emit(["ovVersions", versionChoices])
            self.signals.setDataSignal.emit(["countLabels", instance._nnManager._labels])
            self.signals.setDataSignal.emit(["modelChoices", sorted(confManager.getAvailableZooModels(), key=cmp_to_key(lambda a, b: -1 if a == "mobilenet-ssd" else 1 if b == "mobilenet-ssd" else -1 if a < b else 1))])


    class App(DemoQtGui):
        def __init__(self):
            super().__init__()
            self.running = False
            self.selectedPreview = "color"
            self.dataInitialized = False
            self.appInitialized = False
            self.threadpool = QThreadPool()
            self._demoInstance = Demo(confManager, displayFrames=False)

        def updateArg(self, arg_name, arg_value):
            setattr(confManager.args, arg_name, arg_value)
            self.worker.signals.setDataSignal.emit(["restartRequired", True])

        def start(self):
            self.running = True
            self.worker = Worker(self._demoInstance, selectedPreview=self.selectedPreview)
            self.worker.signals.updatePreviewSignal.connect(self.updatePreview)
            self.worker.signals.setDataSignal.connect(self.setData)
            self.threadpool.start(self.worker)
            if not self.appInitialized:
                self.appInitialized = True
                exit_code = self.startGui()
                self.stop()
                raise SystemExit(exit_code)

        def stop(self):
            current_mxid = self._demoInstance._device.getMxId()
            self.worker.signals.exitSignal.emit()
            self.threadpool.waitForDone(100)
            start = time.time()
            while time.time() - start < 10:
                if current_mxid in list(map(lambda info: info.getMxId(), dai.Device.getAllAvailableDevices())):
                    break
            else:
                raise RuntimeError("Device not available again after 10 seconds!")


        def restartDemo(self):
            self.stop()
            self.start()

        def guiOnDepthConfigUpdate(self, median=None, dct=None, sigma=None, lrcThreshold=None):
            print("median", median)
            self._demoInstance._pm.updateDepthConfig(self._demoInstance._device, median=median, dct=dct, sigma=sigma, lrcThreshold=lrcThreshold)

        def guiOnCameraConfigUpdate(self, name, exposure=None, sensitivity=None, saturation=None, contrast=None, brightness=None, sharpness=None):
            if name == "color":
                fun = self._demoInstance._pm.updateColorCamConfig
            elif name == "left":
                fun = self._demoInstance._pm.updateLeftCamConfig
            else:
                fun = self._demoInstance._pm.updateRightCamConfig
            fun(self._demoInstance._device, exposure, sensitivity, saturation, contrast, brightness, sharpness)

        def guiOnDepthSetupUpdate(self, depthFrom=None, depthTo=None, subpixel=None, lrc=None, extended=None):
            if depthFrom is not None:
                self.updateArg("minDepth", depthFrom)
            if depthTo is not None:
                self.updateArg("maxDepth", depthTo)
            if subpixel is not None:
                self.updateArg("subpixel", subpixel)
            if extended is not None:
                self.updateArg("extendedDisparity", extended)
            if lrc is not None:
                self.updateArg("stereoLrCheck", lrc)

        def guiOnCameraSetupUpdate(self, name, fps=None, resolution=None):
            if fps is not None:
                if name == "color":
                    self.updateArg("rgbFps", fps)
                else:
                    self.updateArg("monoFps", fps)
            if resolution is not None:
                if name == "color":
                    self.updateArg("rgbResolution", resolution)
                else:
                    self.updateArg("monoResolution", resolution)

        def guiOnAiSetupUpdate(self, cnn=None, shave=None, source=None, fullFov=None, sync=None, sbb=None, sbbFactor=None, ov=None, countLabel=None):
            if cnn is not None:
                self.updateArg("cnnModel", cnn)
            if shave is not None:
                self.updateArg("shaves", shave)
            if source is not None:
                self.updateArg("camera", source)
            if fullFov is not None:
                self.updateArg("disableFullFovNn", not fullFov)
            if sync is not None:
                self.updateArg("sync", sync)
            if sbb is not None:
                self.updateArg("spatialBoundingBox", sbb)
            if sbbFactor is not None:
                self.updateArg("sbbScaleFactor", sbbFactor)
            if ov is not None:
                self.updateArg("openvinoVersion", ov)
            if countLabel is not None:
                self.updateArg("countLabel", countLabel)


        def guiOnPreviewChangeSelected(self, selected):
            self.worker.selectedPreview = selected
            self.selectedPreview = selected

    App().start()